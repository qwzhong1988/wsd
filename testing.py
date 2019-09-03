import torch
from fairseq.models.roberta import alignment_utils

from data_preprocessing import FlatLoader, FlatSemCorDataset, load_sense2id
from train import RobertaTrainer
from utils.config import RobertaTransformerConfig
from nltk.corpus import wordnet as wn


class RobertaTest(RobertaTrainer):

    def test(self, loader=None):
        dataset = FlatSemCorDataset('res/wsd-test/se07/se07.xml', 'res/wsd-test/se07/se07.txt')
        loader = FlatLoader(dataset, 10, 100, 'PAD')
        id2sense = {v: k for k, v in self.sense2id.items()}
        with torch.no_grad():
            pred, true, z, lemmas = [], [], [], []
            for step, (b_x, b_p, b_y, b_z) in enumerate(loader):
                scores = self.model(b_x)
                lemmas += [item for seq in b_x for item in seq]
                true += [item for seq in b_y.tolist() for item in seq]
                pred += [item for seq in self._select_senses(scores, b_x, b_p, b_y) for item in seq]
                z += [item for seq in b_z for item in seq]
                never_seen = 0
                tot_err = 0
                not_in_train = 0
                for l, t, p, zz in zip(lemmas, true, pred, z):
                    t_key = id2sense.get(t, '_')
                    p_key = id2sense.get(p, '_')
                    z_keys = [id2sense.get(i, '_') for i in zz]
                    if t_key != '_' and t_key != p_key:
                        t_syn = wn.synset(t_key)
                        p_syn = wn.synset(p_key)
                        train_senses = [id2sense.get(s, s) for s in self.train_sense_map.get(l, [])]
                        count_senses = [(id2sense.get(s, s), self.train_sense_map.get(l, {}).get(s, 0)) for s in self.train_sense_map.get(l, [])]
                        print(f"{l}\t{t_key}: {t_syn.lemma_names()}: {t_syn.definition()}\n"
                              f"\t\t\t{p_key}: {p_syn.lemma_names()}: {p_syn.definition()}\t{z_keys}\n"
                              f"\t\t\t{count_senses}")
                        if l not in self.train_sense_map:
                            print("NOT IN TRAINING\n")
                            not_in_train += 1
                        if t_syn.name() not in train_senses:
                            print("SENSE NEVER SEEN\n")
                            never_seen += 1
                        tot_err += 1
                    else:
                        pass
                        # print(f"{l}")
                if step == 5:
                    break
            print(f"total errors: {tot_err}\n"
                  f"errors with senses never seen: {never_seen} ({(never_seen/tot_err)*100:.2f} %)\n"
                  f"errors for lemmas not seen in training: {not_in_train} ({(not_in_train/tot_err)*100:.2f} %)")
            return self._get_metrics(true, pred, z)


def test0():
    c = RobertaTransformerConfig.from_json_file('conf/roberta_tr_conf_1.json')
    cd = c.__dict__
    cd['is_training'] = False
    t = RobertaTrainer(**cd)
    t.test()


def test1():
    c = RobertaTransformerConfig.from_json_file('conf/roberta_tr_conf_1.json')
    cd = c.__dict__
    cd['is_training'] = False
    t = RobertaTest(**cd)
    t.test()


def test2():
    from fairseq.models.roberta import RobertaModel
    roberta = RobertaModel.from_pretrained('res/roberta.large', checkpoint_file='model.pt')
    roberta.eval()

    dataset = FlatSemCorDataset('res/wsd-test/se07/se07.xml', 'res/wsd-test/se07/se07.txt')
    loader = FlatLoader(dataset, 32, 100, 'PAD')
    sense2id = load_sense2id()
    pred, true, z = [], [], []
    for step, (b_x, b_p, b_y, b_z) in enumerate(loader):
        for seq in b_x:
            sent = ' '.join(seq)
            encoded = roberta.encode(sent)
            alignment = alignment_utils.align_bpe_to_words(roberta, encoded, seq)
            features = roberta.extract_features(encoded, return_all_hiddens=False)
            features = features.squeeze(0)
            aligned = alignment_utils.align_features_to_words(roberta, features, alignment)[1:-1]

            print(aligned.shape)
            print(len(seq))
    print('\nDone.')


if __name__ == '__main__':
    test1()
