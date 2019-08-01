import math

import torch
from allennlp.modules.elmo import Elmo
from pytorch_transformers import BertModel, BertConfig
from torch import nn
from torch.nn import CrossEntropyLoss

from models import Attention, WSDTransformerEncoder
from utils.config import TransformerConfig
from utils.util import pos2id


class BaselineWSD(nn.Module):

    def __init__(self, num_senses: int, max_len: int):
        super().__init__()
        self.tagset_size = num_senses
        self.win_size = max_len
        self.pad_tag_index = 0
        self.ce_loss = CrossEntropyLoss(ignore_index=0)

    def forward(self, *inputs):
        pass

    def loss(self, scores, tags):
        y_true = tags.view(-1)
        scores = scores.view(-1, self.tagset_size)
        return self.ce_loss(scores, y_true)


class SimpleWSD(BaselineWSD):

    _ELMO_OPTIONS = 'res/elmo/elmo_2x1024_128_2048cnn_1xhighway_options.json'
    _ELMO_WEIGHTS = 'res/elmo/elmo_2x1024_128_2048cnn_1xhighway_weights.hdf5'
    _ELMO_SIZE = 128
    _HIDDEN_SIZE = 1024
    _NUM_LAYERS = 2
    _BATCH_SIZE = 32

    def __init__(self,
                 num_senses,
                 max_len,
                 elmo_weights=_ELMO_WEIGHTS,
                 elmo_options=_ELMO_OPTIONS,
                 elmo_size=_ELMO_SIZE,
                 hidden_size=_HIDDEN_SIZE,
                 num_layers=_NUM_LAYERS,
                 batch_size=_BATCH_SIZE):
        super().__init__(num_senses, max_len)
        self.elmo_weights = elmo_weights
        self.elmo_options = elmo_options
        self.elmo_size = elmo_size
        self.hidden_size = hidden_size
        self.num_layers = num_layers
        self.elmo = Elmo(self.elmo_options,
                         self.elmo_weights,
                         2, dropout=0)
        self.embedding_size = 2 * self.elmo_size
        self.lstm = nn.LSTM(self.embedding_size,
                            hidden_size=self.hidden_size,
                            num_layers=self.num_layers,
                            bidirectional=True,
                            batch_first=True)
        self.attention = Attention(self.hidden_size)
        self.output_dense = nn.Linear(self.hidden_size * 4, self.tagset_size)  # 2 directions * (state + attn)
        self.batch_size = batch_size
        self.h, self.cell = self.init_hidden(self.batch_size)

    def init_hidden(self, batch_size):
        self.batch_size = batch_size
        return (torch.zeros(self.num_layers * 2, self.batch_size, self.hidden_size),  # hidden state
                torch.zeros(self.num_layers * 2, self.batch_size, self.hidden_size))  # cell state

    def forward(self, char_ids, lengths):

        embeddings = self.elmo(char_ids)
        x = embeddings['elmo_representations'][1]
        hidden_states, (self.h, self.cell) = self.lstm(x, (self.h, self.cell))
        out = hidden_states

        y = self.attention(out)
        y = y.contiguous().view(-1, y.shape[2])
        y = self.output_dense(y)
        y = nn.LogSoftmax(dim=1)(y)
        tag_scores = y.view(self.batch_size, self.win_size, self.tagset_size)
        return tag_scores


class ElmoTransformerWSD(nn.Module):

    _ELMO_OPTIONS = ''
    _ELMO_WEIGHTS = ''

    def __init__(self):
        super().__init__()
        pass

    def forward(self, *inputs):
        pass


class BertTransformerWSD(BaselineWSD):

    def __init__(self, device, num_senses, max_len, config: TransformerConfig):
        super().__init__(num_senses, max_len)
        self.bert_config = BertConfig.from_pretrained('bert-base-uncased')
        self.bert_embedding = BertModel(self.bert_config)
        self.config = config
        self.pos_embed = nn.Embedding(len(pos2id), self.config.pos_embed_dim, padding_idx=0)
        self.transformer_layer = WSDTransformerEncoder(self.config)
        self.output_dense = nn.Linear(self.config.encoder_embed_dim, self.tagset_size)
        self.device = device
        self.scale = math.sqrt(self.config.encoder_embed_dim)

    def forward(self, token_ids, lengths, slices, text_lengths, pos_tags):
        """
        :param pos_tags:
        :param text_lengths: List[int] shape = `(batch)`
        :param slices: List[Slice]
        :param lengths: List[int] shape = `(batch)`
        :param token_ids: (Tensor) shape `(batch, seq_len)`
        :return:
        """
        max_len = token_ids.shape[1]
        attention_mask = torch.arange(max_len)\
                              .expand(len(lengths), max_len)\
                              .to(self.device) < lengths.unsqueeze(1)
        max_text_len = text_lengths.max().item()
        transformer_mask = 1 - (torch.arange(max_text_len)
                                     .expand(len(text_lengths), max_text_len)
                                     .to(self.device) < text_lengths.unsqueeze(1))
        x, _ = self.bert_embedding(token_ids, attention_mask=attention_mask)
        # aggregate bert sub-words and pad to max len
        x = torch.nn.utils.rnn.pad_sequence(
            [torch.cat([torch.mean(x[i, sl, :], dim=-2)
                        for sl in slices[i]])
                  .reshape(-1, self.config.encoder_embed_dim - self.config.pos_embed_dim)
             for i in range(x.shape[0])
             ],
            batch_first=True)
        x_p = self.pos_embed(pos_tags)
        x = torch.cat([x, x_p], dim=-1)
        x = x * self.scale  # embedding scale
        x = x.transpose(1, 0)  # make batch second dim for transformer layer
        for _ in range(self.config.num_layers):
            x = self.transformer_layer(x, transformer_mask)
        x = x.transpose(1, 0)  # restore batch first
        x = self.output_dense(x)
        return x


class WSDNet(nn.Module):
    """
    Multi-Task network for WSD
    """

    def __init__(self):
        super().__init__()
        pass

    def forward(self, *inputs):
        pass
