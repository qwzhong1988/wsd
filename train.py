import argparse
import datetime
import logging
import os
import random
import warnings
from itertools import count
from typing import Set, Iterable, List

import numpy as np
import torch

try:
    from apex import amp
    AMP = True
except ImportError:
    AMP = False
from nltk.corpus import wordnet as wn
from sklearn.exceptions import UndefinedMetricWarning
from sklearn.metrics import classification_report, f1_score
from torch import optim, nn
from torch.nn.utils import clip_grad_norm_
from torch.utils.tensorboard import SummaryWriter

from data_preprocessing import FlatSemCorDataset, load_sense2id, FlatLoader, CachedEmbedLoader
from utils import util
from utils.config import RobertaTransformerConfig, WSDNetXConfig, RDenseConfig, WSDDenseConfig
from utils.util import NOT_AMB_SYMBOL, telegram_on_failure, telegram_send, Randomized
from wsd import ElmoTransformerWSD, RobertaTransformerWSD, BertTransformerWSD, BaselineWSD, WSDNetX, \
    RobertaDenseWSD, WSDNetDense

warnings.filterwarnings("ignore", category=UndefinedMetricWarning)
torch.manual_seed(42)
np.random.seed(42)
random.seed(42)
START_EVAL_EPOCH = 14
SV_TRAIN_EPOCHS = 2
BATCH_MUL = CachedEmbedLoader.SINGLE
RANDOMIZE = False


class BaseTrainer:

    def __init__(self,
                 num_epochs=40,
                 batch_size=32,
                 accumulation_steps=4,
                 window_size=64,
                 learning_rate=0.0001,
                 checkpoint_path='saved_weights/baseline_elmo_checkpoint.pt',
                 log_interval=400,
                 train_data='res/wsd-train/semcor+glosses_data.xml',
                 train_tags='res/wsd-train/semcor+glosses_tags.txt',
                 eval_data='res/wsd-test/se07/se07.xml',
                 eval_tags='res/wsd-test/se07/se07.txt',
                 test_data='res/wsd-train/test_data.xml',
                 test_tags='res/wsd-train/test_tags.txt',
                 sense_dict='res/dictionaries/senses.txt',
                 report_path='logs/baseline_elmo_report.txt',
                 pad_symbol='<pad>',
                 is_training=True,
                 mixed_precision='O0',
                 multi_gpu=False,
                 cache_embeddings=False,
                 cache_path='res/cache',
                 embed_model_path='',
                 **kwargs):

        self.num_epochs = num_epochs
        self.batch_size = batch_size
        self.accumulation_steps = accumulation_steps
        self.window_size = window_size
        self.learning_rate = learning_rate
        self.checkpoint_path = checkpoint_path
        self.log_interval = log_interval
        self._plot_server = None
        self.report_path = report_path
        self.model = None
        self.optimizer = None
        self.min_loss = np.inf
        self.data_loader = None
        self.eval_loader = None
        self.test_loader = None
        self.train_sense_map = {}
        self.last_step = 0
        self.multi_gpu = multi_gpu
        self.cache_embeddings = cache_embeddings
        self.cache_path = cache_path
        self.embed_model_path = embed_model_path
        self.cache_batch_size = self.batch_size * 2 if BATCH_MUL == CachedEmbedLoader.HALF \
            else self.batch_size // BATCH_MUL
        self.best_model_path = self.checkpoint_path + '.best'
        self.sense2id = load_sense2id(sense_dict, train_tags, test_tags)
        self.all_sense_ids = set(range(len(self.sense2id) + 1))
        logging.debug('Loaded sense2id vocab')
        self.pad_symbol = pad_symbol
        self.rnd_loader = None
        self.eval_rnd_loader = None
        self.test_rnd_loader = None
        self.impossible_senses_map = {}
        self.na_padded = None

        dataset = FlatSemCorDataset(train_data, train_tags)

        self.train_sense_map = dataset.train_sense_map
        self.device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
        logging.info(f'Device is {self.device}')
        self._build_model()
        self.has_master_params = False
        if mixed_precision == 'O1' or mixed_precision == 'O2':
            logging.info("Using mixed precision model.")
            self.has_master_params = True
        self.mixed = mixed_precision
        logging.info(f'Number of parameters: {sum([p.numel() for p in self.model.parameters()])}')
        logging.info(f'Number of trainable parameters: '
                     f'{sum([p.numel() for p in self.model.parameters() if p.requires_grad])}')
        if torch.cuda.device_count() > 1 and self.multi_gpu:
            self.model = nn.DataParallel(self.model)

        if is_training:
            self.data_loader = FlatLoader(dataset, batch_size=self.batch_size, win_size=self.window_size,
                                          pad_symbol=self.pad_symbol, overlap=0)
            self.cached_data_loader = CachedEmbedLoader(self.device, f'{self.cache_path}_{self.cache_batch_size}.npz',
                                                        self.embed_model_path, BATCH_MUL, self.batch_size, self.data_loader) \
                if self.cache_embeddings else count()
            self._setup_training(eval_data, eval_tags)
        else:
            self._setup_testing(test_data, test_tags)

    def _build_model(self):
        raise NotImplementedError("Do not use base class, use concrete classes instead.")

    def _setup_training(self, eval_data, eval_tags):
        eval_dataset = FlatSemCorDataset(data_path=eval_data, tags_path=eval_tags)
        self.eval_loader = FlatLoader(eval_dataset, batch_size=self.batch_size, win_size=self.window_size,
                                      pad_symbol=self.pad_symbol, with_word_ids=True)
        self.cached_eval_loader = CachedEmbedLoader(self.device, f'{self.cache_path}_eval_{self.cache_batch_size}.npz',
                                                    self.embed_model_path, BATCH_MUL, self.batch_size, self.eval_loader,
                                                    to_device=True) \
            if self.cache_embeddings else count()
        self._warm_up_sense_ids(self.eval_loader)
        self.model.to(self.device)
        self.optimizer = optim.Adam(self.model.parameters(), lr=self.learning_rate)
        # self.optimizer = optim.AdamW(self.model.parameters(), lr=self.learning_rate, amsgrad=True)
        # Use apex to make model possibly faster.
        loss_scale = 1 if self.mixed == 'O0' else 'dynamic'
        (self.model, _), self.optimizer = amp.initialize(self.model, self.optimizer,
                                                         opt_level=self.mixed, loss_scale=loss_scale) \
            if AMP else (self.model, None), self.optimizer
        self._maybe_load_checkpoint()

    def _setup_testing(self, test_data, test_tags):
        test_dataset = FlatSemCorDataset(data_path=test_data, tags_path=test_tags)
        self.test_loader = FlatLoader(test_dataset, batch_size=self.batch_size, win_size=self.window_size,
                                      pad_symbol=self.pad_symbol, with_word_ids=True)
        self.cached_test_loader = CachedEmbedLoader(self.device, f'{self.cache_path}_test_{self.cache_batch_size}.npz',
                                                    self.embed_model_path, BATCH_MUL, self.batch_size, self.test_loader,
                                                    to_device=True) \
            if self.cache_embeddings else count()
        self._warm_up_sense_ids(self.test_loader)
        self._load_best()
        self.model.eval()
        self.model.to(self.device)

    def train_epoch(self, epoch_i):
        step, local_step, flag = 0, 0, False
        self.model.zero_grad()
        for step, ((b_x, b_p, b_y, b_z), b_x_e) in enumerate(self.rnd_loader, self.last_step):
            b_x_e = b_x_e if self.cache_embeddings else None
            scores, loss = self.model(b_x, cached_embeddings=b_x_e.to(self.device), tags=b_y)
            if AMP:
                with amp.scale_loss(loss, self.optimizer) as scaled_loss:
                    scaled_loss.backward()
            else:
                loss.sum().backward()
            loss /= self.accumulation_steps
            parameters = self.model.parameters() if not self.has_master_params else amp.master_params(self.optimizer)
            clip_grad_norm_(parameters=parameters, max_norm=1.0)

            if (step + 1) % self.accumulation_steps == 0:
                local_step += 1
                self._log(local_step, loss.sum(), epoch_i)
                self.optimizer.step()  # update the weights
                self.model.zero_grad()
                flag = False
            else:
                flag = True
        if flag:
            self._log(local_step + 1, loss.sum(), epoch_i)
            self.optimizer.step()  # update the weights
        self.last_step += step

    def train(self):
        print(self.model)
        self.model.train()
        self.rnd_loader = zip(self.data_loader, self.cached_data_loader)
        if RANDOMIZE:
            self.rnd_loader = Randomized(self.rnd_loader)
            del self.data_loader
            del self.cached_data_loader
        start = datetime.datetime.now()
        for epoch in range(self.last_epoch + 1, self.num_epochs + 1):
            end = datetime.datetime.now()
            logging.info(f'Epoch: {epoch} - time: {end - start}')
            start = end
            self.train_epoch(epoch)
            if epoch >= START_EVAL_EPOCH and BATCH_MUL == CachedEmbedLoader.HALF:
                self._set_global_lr(self.learning_rate / 2)
                if epoch >= 20:
                    self._set_global_lr(0.0001)
            if not RANDOMIZE:  # reinitialize iterators
                self.rnd_loader = zip(self.data_loader, self.cached_data_loader)
            if epoch > SV_TRAIN_EPOCHS:
                try:  # make sparse sv matrix non trainable
                    self.model.vals.requires_grad = False
                except AttributeError:
                    pass

    def _log(self, step, loss, epoch_i):
        if step % self.log_interval == 0:
            log_str = f'Loss: {loss.item():.4f}'
            self._plot('Train_loss', loss.item(), step)
            self._gpu_mem_info()
            self._maybe_checkpoint(loss, epoch_i)
            if epoch_i >= START_EVAL_EPOCH:  # or epoch_i == 1:
                f1 = self._evaluate(epoch_i)
                self._plot('Dev_F1', f1, step)
                self.model.train()  # return to train mode after evaluation
                log_str += f'\t\t\tF1: {f1:.5f}'
            logging.info(log_str)

    def test(self, loader=None):
        """
        """
        test = loader is None
        if test:
            loader = self.test_loader
            cache_loader = self.cached_test_loader
        else:
            cache_loader = self.cached_eval_loader
        with torch.no_grad():
            pred, true, also_true, w_ids, pos_tags = [], [], [], [], []
            for step, ((b_x, b_p, b_y, b_z, b_ids), b_x_e) in enumerate(zip(loader, cache_loader)):
                try:
                    b_x_e = b_x_e if self.cache_embeddings else None
                    scores = self.model(b_x, cached_embeddings=b_x_e)
                except TypeError:  # model doesn't support embeddings caching
                    scores = self.model(b_x)
                true += [item for seq in b_y.tolist() for item in seq]
                pred += [item for seq in self._select_senses(scores, b_x, b_p, b_y) for item in seq]
                also_true += [item for seq in b_z for item in seq]
                w_ids += [item for seq in b_ids for item in seq]
                pos_tags += [util.id2wnpos[item] for seq in b_p for item in seq]

            metrics = self._get_metrics(true, pred, also_true)
            if test:
                logging.info(f'F1: {metrics:.6f}')
                self._print_predictions(pred, w_ids)  # save in Raganato's scorer format.
                for pos in sorted(set(util.id2wnpos.values())):
                    true_, pred_, also_true_ = [], [], []
                    for i in range(len(true)):
                        if pos_tags[i] == pos and true[i] != NOT_AMB_SYMBOL:
                            true_.append(true[i])
                            pred_.append(pred[i])
                            also_true_.append(also_true[i])
                    if len(true_) > 0:
                        f1 = self._get_metrics(true_, pred_, also_true_)
                        logging.info(f'F1 on {pos}: {f1:.6f}')
            return metrics

    def _evaluate(self, num_epoch):
        self.model.eval()
        f1 = self.test(self.eval_loader)
        self._save_best(f1, num_epoch)
        return f1

    def _select_senses(self, b_scores, b_str, b_pos, b_labels) -> Iterable:
        """
        :param b_scores: shape = (batch_s x win_s x sense_vocab_s)
        :param b_str:
        :param b_pos:
        :return:
        """
        b_impossible_senses = []
        # we will set to 0 senses not in WordNet for given lemma.
        for i in range(len(b_str)):
            impossible_senses = []
            for j in range(len(b_str[i])):
                if b_labels[i, j] == NOT_AMB_SYMBOL:
                    impossible_senses.append(self.na_padded)
                else:
                    impossible_senses.append(self.impossible_senses_map[(b_str[i][j], b_pos[i][j])])
            b_impossible_senses.append(impossible_senses)
        b_impossible_senses = torch.tensor(b_impossible_senses).to(b_scores.get_device())
        b_scores.scatter_(-1, b_impossible_senses, torch.min(b_scores))
        return torch.argmax(b_scores, -1).cpu().tolist()

    def _set2padded(self, s: Set[int]):
        arr = np.array(list(s))
        return np.pad(arr, (0, len(self.sense2id) + 1 - len(s)), 'edge')

    def _warm_up_sense_ids(self, loader: FlatLoader):

        def to_ids(synsets):
            return set([self.sense2id.get(x.name(), 0) for x in synsets]) - {0}

        logging.info("Warming up lemma+pos to synsets map...")
        self.na_padded = self._set2padded(self.all_sense_ids)
        for b_x, b_p, b_y, b_z, b_ids in loader:
            for i, sent in enumerate(b_x):
                for j, lemma in enumerate(sent):
                    if b_y[i, j] != NOT_AMB_SYMBOL:
                        if (lemma, b_p[i][j]) not in self.impossible_senses_map:
                            sense_ids = to_ids(wn.synsets(lemma, pos=util.id2wnpos[b_p[i][j]]))
                            padded = self._set2padded(self.all_sense_ids - sense_ids)
                            self.impossible_senses_map[(lemma, b_p[i][j])] = padded

    def _print_predictions(self, pred_indices: List[int], amb_word_ids: List[str]):
        output_path = self.report_path.replace('report', 'results')
        id2sense = {v: k for k, v in self.sense2id.items()}
        with open(output_path, 'w') as f:
            for w_id, pred in zip(amb_word_ids, pred_indices):
                if w_id != '#':
                    print(f"{w_id} {id2sense[pred]}", file=f)

    def _print_metrics(self, true_eval, pred_eval):
        with open(self.report_path, 'w') as fo:
            print(classification_report(
                true_eval,
                pred_eval,
                digits=3),
                file=fo)
        f1 = f1_score(true_eval, pred_eval, average='micro')
        return f1

    def _get_metrics(self, true, pred, alternatives=None):
        true_eval, pred_eval = [], []
        for i in range(len(true)):
            if true[i] == NOT_AMB_SYMBOL:
                continue
            else:
                if alternatives is None or pred[i] in alternatives[i]:
                    true_eval.append(pred[i])
                else:
                    true_eval.append(true[i])
                pred_eval.append(pred[i])
        return self._print_metrics(true_eval, pred_eval)

    def _maybe_checkpoint(self, loss, epoch_i):
        current_loss = loss.item()
        if current_loss < self.min_loss:
            min_loss = current_loss
            ad = amp.state_dict() if AMP else {}
            torch.save({
                'epoch': epoch_i,
                'last_step': self.last_step,
                'model_state_dict': self.model.state_dict(),
                'optimizer_state_dict': self.optimizer.state_dict(),
                'amp': ad,
                'current_loss': current_loss,
                'min_loss': min_loss,
                'f1': self.best_f1_micro
            }, self.checkpoint_path)

    def _maybe_load_checkpoint(self):
        if os.path.exists(self.checkpoint_path):
            checkpoint = torch.load(self.checkpoint_path)
            self.model.load_state_dict(checkpoint['model_state_dict'])
            self.optimizer.load_state_dict(checkpoint['optimizer_state_dict'])
            if AMP:
                amp.load_state_dict(checkpoint['amp'])
            self.last_epoch = checkpoint['epoch']
            self.last_step = checkpoint['last_step']
            self.min_loss = checkpoint['min_loss']
            self.best_f1_micro = checkpoint['f1']
            logging.info(f"Loaded checkpoint from: {self.checkpoint_path}")
            logging.debug(f"Last epoch: {self.last_epoch}")
            logging.debug(f"Last best F1: {self.best_f1_micro}")
            logging.debug(f"Min loss registered: {self.min_loss}")
            if self.last_epoch >= self.num_epochs:
                logging.warning("Training finished for this checkpoint")
        else:
            logging.debug(f"No checkpoint found in {self.checkpoint_path}")
            self.last_epoch = 0
            self.last_step = 0
            self.min_loss = 1e3
            self.best_f1_micro = 0.0

    def _load_best(self):
        if os.path.exists(self.best_model_path):
            checkpoint = torch.load(self.best_model_path, map_location=str(self.device))
            logging.info(f"Loading best model achieving {checkpoint['f1']:.5f} on validation set.")
            try:
                self.model.load_state_dict(checkpoint['model_state_dict'])
            except RuntimeError:
                self.model.load_state_dict(util.from_multigpu_state_dict(checkpoint['model_state_dict']))
        else:
            raise ValueError(f"Could not find any best model checkpoint: {self.best_model_path}")

    def _save_best(self, f1, epoch_i):
        if f1 >= self.best_f1_micro:
            self.best_f1_micro = f1
            if torch.cuda.device_count() > 1 and self.multi_gpu:
                state_dict = self.model.module.state_dict()
            else:
                state_dict = self.model.state_dict()
            torch.save({
                'epoch': epoch_i,
                'model_state_dict': state_dict,
                'f1': f1
            }, self.best_model_path)

    def _plot(self, name, value, step):
        if not self._plot_server:
            self._plot_server = SummaryWriter(log_dir='logs')
        self._plot_server.add_scalar(name, value, step)

    @staticmethod
    def _gpu_mem_info():
        if torch.cuda.is_available():  # check if memory is leaking
            logging.debug(f'Allocated GPU memory: '
                          f'{torch.cuda.memory_allocated() / 1_000_000} MB')

    def _set_global_lr(self, lr: float):
        for g in self.optimizer.param_groups:
            g['lr'] = max(lr, 0.0001)


class ElmoLSTMTrainer(BaseTrainer):

    def _build_model(self):
        self.model = BaselineWSD(self.device, len(self.sense2id) + 1, self.window_size,
                                 self.elmo_weights, self.elmo_options, self.elmo_size,
                                 self.hidden_size, self.num_layers)

    def __init__(self,
                 hidden_size=1024,
                 num_layers=2,
                 learning_rate=0.001,
                 elmo_weights='res/elmo/elmo_2x1024_128_2048cnn_1xhighway_weights.hdf5',
                 elmo_options='res/elmo/elmo_2x1024_128_2048cnn_1xhighway_options.json',
                 elmo_size=128,
                 **kwargs):
        self.learning_rate = learning_rate
        self.elmo_weights = elmo_weights
        self.elmo_options = elmo_options
        self.elmo_size = elmo_size
        self.num_layers = num_layers
        self.hidden_size = hidden_size
        super().__init__(**kwargs)


class ElmoTransformerTrainer(BaseTrainer):

    def __init__(self,
                 num_layers=2,
                 elmo_weights='res/elmo/elmo_2x1024_128_2048cnn_1xhighway_weights.hdf5',
                 elmo_options='res/elmo/elmo_2x1024_128_2048cnn_1xhighway_options.json',
                 elmo_size=128,
                 d_model=512,
                 num_heads=4,
                 **kwargs):
        self.elmo_weights = elmo_weights
        self.elmo_options = elmo_options
        self.elmo_size = elmo_size
        self.num_layers = num_layers
        self.d_model = d_model
        self.num_heads = num_heads
        super().__init__(**kwargs)

    def _build_model(self):
        self.model = ElmoTransformerWSD(self.device, len(self.sense2id) + 1, self.window_size, self.elmo_weights,
                                        self.elmo_options, self.elmo_size, self.d_model,
                                        self.num_heads, self.num_layers)


class RobertaTrainer(BaseTrainer):

    def __init__(self,
                 num_layers=2,
                 d_embeddings=1024,
                 d_model=2048,
                 num_heads=4,
                 model_path='res/roberta.large',
                 **kwargs):
        self.num_layers = num_layers
        self.d_model = d_model
        self.d_embeddings = d_embeddings
        self.num_heads = num_heads
        self.model_path = model_path
        super().__init__(**kwargs)

    def _build_model(self):
        self.model = RobertaTransformerWSD(self.device, len(self.sense2id) + 1, self.window_size,
                                           self.model_path, self.d_embeddings, self.d_model,
                                           self.num_heads, self.num_layers, self.cache_embeddings)


class BertTransformerTrainer(BaseTrainer):

    def __init__(self,
                 d_model=512,
                 num_layers=2,
                 num_heads=4,
                 bert_model='bert-large-cased',
                 **kwargs):
        self.num_layers = num_layers
        self.d_model = d_model
        self.num_heads = num_heads
        self.bert_model = bert_model
        super().__init__(**kwargs)

    def _build_model(self):
        self.model = BertTransformerWSD(self.device, len(self.sense2id) + 1, self.window_size,
                                        self.d_model, self.num_heads, self.num_layers,
                                        self.bert_model)


class WSDNetXTrainer(BaseTrainer):

    def __init__(self,
                 num_layers=2,
                 d_embeddings=1024,
                 d_model=2048,
                 num_heads=4,
                 model_path='res/roberta.large',
                 output_vocab: str = 'res/dictionaries/syn_lemma_vocab.txt',
                 sense_lemmas: str = 'res/dictionaries/sense_lemmas.txt',
                 sv_trainable: bool = True,
                 **kwargs):
        self.num_layers = num_layers
        self.d_model = d_model
        self.d_embeddings = d_embeddings
        self.num_heads = num_heads
        self.model_path = model_path
        self.output_vocab = output_vocab
        self.sense_lemmas = sense_lemmas
        self.sv_trainable = sv_trainable
        super().__init__(**kwargs)

    def _build_model(self):
        self.model = WSDNetX(self.device, len(self.sense2id) + 1, self.window_size,
                             self.model_path, self.d_embeddings, self.d_model,
                             self.num_heads, self.num_layers, self.output_vocab,
                             self.sense_lemmas, self.cache_embeddings, sv_trainable=self.sv_trainable)


class RDenseTrainer(BaseTrainer):

    def __init__(self,
                 num_layers=2,
                 d_embeddings=1024,
                 hidden_dim=512,
                 model_path='res/roberta.large',
                 **kwargs):
        self.num_layers = num_layers
        self.hidden_dim = hidden_dim
        self.d_embeddings = d_embeddings
        self.model_path = model_path
        super().__init__(**kwargs)

    def _build_model(self):
        self.model = RobertaDenseWSD(self.device, len(self.sense2id) + 1, self.window_size,
                                     self.model_path, self.d_embeddings, self.hidden_dim, self.cache_embeddings)


class WSDDenseTrainer(BaseTrainer):

    def __init__(self,
                 num_layers=2,
                 d_embeddings=1024,
                 hidden_dim=512,
                 model_path='res/roberta.large',
                 output_vocab='res/dictionaries/syn_lemma_vocab.txt',
                 sense_lemmas='res/dictionaries/sense_lemmas.txt',
                 sv_trainable=False,
                 **kwargs):
        self.num_layers = num_layers
        self.hidden_dim = hidden_dim
        self.d_embeddings = d_embeddings
        self.model_path = model_path
        self.output_vocab = output_vocab
        self.sense_lemmas = sense_lemmas
        self.sv_trainable = sv_trainable
        super().__init__(**kwargs)

    def _build_model(self):
        self.model = WSDNetDense(self.device, len(self.sense2id) + 1, self.window_size,
                                 self.model_path, self.d_embeddings, self.hidden_dim, self.cache_embeddings,
                                 self.output_vocab, self.sense_lemmas, sv_trainable=self.sv_trainable)


if __name__ == '__main__':

    parser = argparse.ArgumentParser(description="Train with different models and options")
    parser.add_argument("-m", "--model", type=str, help="model name",
                        required=True, choices=('rtransform', 'wsdnetx', 'rdense', 'wsddense'))
    parser.add_argument("-c", "--config", type=str, help="config JSON file path", required=True)
    parser.add_argument("-t", "--test", action='store_true', help="If test else run training")
    parser.add_argument("-d", "--debug", action='store_true', help="Print debug information")
    parser.add_argument("-x", "--clean", action='store_true', help="Clear old saved weights.")
    parser.add_argument("-g", "--multi-gpu", action='store_true', help="Use all available GPUs.")
    parser.add_argument("-l", "--log", type=str, help="log file name")
    parser.add_argument("-o", "--mixed-level", type=str, help="Train with mixed precision floats.",
                        default='O0', choices=('O0', 'O1', 'O2'))
    parser.add_argument("-z", "--cache", type=str, help="Embeddings cache", default='res/cache')
    parser.add_argument("-s", "--sequential", action='store_true', help="Feed batches as read sequentially.")
    args = parser.parse_args()
    log_level = logging.DEBUG if args.debug else logging.INFO
    if args.log:
        logging.basicConfig(filename=args.log, level=log_level, format='%(asctime)s: %(levelname)s: %(message)s')
    else:
        logging.basicConfig(level=log_level, format='%(asctime)s: %(levelname)s: %(message)s')
    logging.info(f'Initializing... model = {args.model}')
    if args.config.endswith('_half.json'):
        BATCH_MUL = CachedEmbedLoader.HALF
    RANDOMIZE = not args.sequential
    c, t = None, None
    if args.model == 'rtransform':
        c = RobertaTransformerConfig.from_json_file(args.config)
    elif args.model == 'wsdnetx':
        c = WSDNetXConfig.from_json_file(args.config)
    elif args.model == 'rdense':
        c = RDenseConfig.from_json_file(args.config)
    elif args.model == 'wsddense':
        c = WSDDenseConfig.from_json_file(args.config)
    cd = c.__dict__
    cd['is_training'] = not args.test
    cd['mixed_precision'] = args.mixed_level
    cd['multi_gpu'] = args.multi_gpu
    cd['cache_path'] = args.cache
    if args.clean and os.path.exists(cd['checkpoint_path']):
        os.remove(cd['checkpoint_path'])
        if os.path.exists(cd['checkpoint_path'] + '.best'):
            os.remove(cd['checkpoint_path'] + '.best')
    if args.model == 'rtransform':
        t = RobertaTrainer(**cd)
    elif args.model == 'wsdnetx':
        t = WSDNetXTrainer(**cd)
    elif args.model == 'rdense':
        t = RDenseTrainer(**cd)
    elif args.model == 'wsddense':
        t = WSDDenseTrainer(**cd)
    if args.test:
        telegram_on_failure(t.test)
    else:
        telegram_on_failure(t.train)

