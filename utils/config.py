from dataclasses import dataclass
from dataclasses_json import dataclass_json


@dataclass_json
@dataclass
class Config(object):

    batch_size: int = 32
    num_epochs: int = 40

    train_data: str = 'res/wsd-train/semcor+glosses_data.xml'
    train_tags: str = 'res/wsd-train/semcor+glosses_tags.txt'
    eval_data:  str = 'res/wsd-test/se07/se07.xml'
    eval_tags:  str = 'res/wsd-test/se07/se07.txt'
    test_data:  str = 'res/wsd-train/test_data.xml'
    test_tags:  str = 'res/wsd-train/test_tags.txt'

    log_interval: int = 400


@dataclass_json
@dataclass
class ElmoConfig(Config):

    hidden_size: int = 1024
    num_layers:  int = 2

    elmo_weights: str = ''
    elmo_options: str = ''
    elmo_size: int = ''

    learning_rate: float = 0.001

    checkpoint_path: str = 'saved_weights/baseline_elmo_checkpoint.pt'
    report_path: str = 'logs/baseline_elmo_report.txt'

    @staticmethod
    def from_json_file(file_name, **kwargs):
        with open(file_name) as f:
            return ElmoConfig.from_json(f.read(), **kwargs)


@dataclass_json
@dataclass
class TransformerConfig(Config):

    checkpoint_path: str = 'saved_weights/transformer_wsd_checkpoint.pt'
    report_path: str = 'logs/transformer_wsd_report.txt'

    @staticmethod
    def from_json_file(file_name, **kwargs):
        with open(file_name) as f:
            return TransformerConfig.from_json(f.read(), **kwargs)


@dataclass_json
@dataclass
class WSDNetConfig(Config):

    learning_rate: float = 0.0001

    checkpoint_path: str = 'saved_weights/wsdnet_checkpoint.pt'
    report_path: str = 'logs/wsdnet_report.txt'

    @staticmethod
    def from_json_file(file_name, **kwargs):
        with open(file_name) as f:
            return WSDNetConfig.from_json(f.read(), **kwargs)


# Test
if __name__ == "__main__":
    c = ElmoConfig.from_json_file("../conf/baseline_elmo_conf.json")
    print(c.to_json())
