import json
from pathlib import Path

import torch
from omegaconf import OmegaConf
from tqdm import tqdm

from ultralytics.nn.autobackend import AutoBackend
from ultralytics.yolo.data.utils import check_dataset, check_dataset_yaml
from ultralytics.yolo.utils import DEFAULT_CONFIG, LOGGER, RANK, TQDM_BAR_FORMAT
from ultralytics.yolo.utils.files import increment_path
from ultralytics.yolo.utils.ops import Profile
from ultralytics.yolo.utils.torch_utils import check_imgsz, de_parallel, select_device, smart_inference_mode


class BaseValidator:
    """
    Base validator class.
    """

    def __init__(self, dataloader=None, save_dir=None, pbar=None, logger=None, args=None):
        self.dataloader = dataloader
        self.pbar = pbar
        self.logger = logger or LOGGER
        self.args = args or OmegaConf.load(DEFAULT_CONFIG)
        self.model = None
        self.data = None
        self.device = None
        self.batch_i = None
        self.training = True
        self.speed = None
        self.jdict = None

        project = self.args.project or f"runs/{self.args.task}"
        name = self.args.name or f"{self.args.mode}"
        self.save_dir = save_dir or increment_path(Path(project) / name,
                                                   exist_ok=self.args.exist_ok if RANK in {-1, 0} else True)
        (self.save_dir / 'labels' if self.args.save_txt else self.save_dir).mkdir(parents=True, exist_ok=True)

    @smart_inference_mode()
    def __call__(self, trainer=None, model=None):
        """
        Supports validation of a pre-trained model if passed or a model being trained
        if trainer is passed (trainer gets priority).
        """
        self.training = trainer is not None
        if self.training:
            self.device = trainer.device
            self.data = trainer.data
            model = trainer.ema.ema or trainer.model
            self.args.half &= self.device.type != 'cpu'
            model = model.half() if self.args.half else model.float()
            self.model = model
            self.loss = torch.zeros_like(trainer.loss_items, device=trainer.device)
            self.args.plots = trainer.epoch == trainer.epochs - 1  # always plot final epoch
        else:
            assert model is not None, "Either trainer or model is needed for validation"
            self.device = select_device(self.args.device, self.args.batch_size)
            self.args.half &= self.device.type != 'cpu'
            model = AutoBackend(model, device=self.device, dnn=self.args.dnn, fp16=self.args.half)
            self.model = model
            stride, pt, jit, engine = model.stride, model.pt, model.jit, model.engine
            imgsz = check_imgsz(self.args.imgsz, s=stride)
            if engine:
                self.args.batch_size = model.batch_size
            else:
                self.device = model.device
                if not pt and not jit:
                    self.args.batch_size = 1  # export.py models default to batch-size 1
                    self.logger.info(
                        f'Forcing --batch-size 1 square inference (1,3,{imgsz},{imgsz}) for non-PyTorch models')

            if isinstance(self.args.data, str) and self.args.data.endswith(".yaml"):
                data = check_dataset_yaml(self.args.data)
            else:
                data = check_dataset(self.args.data)
            self.dataloader = self.dataloader or \
                              self.get_dataloader(data.get("val") or data.set("test"), self.args.batch_size)

        model.eval()

        dt = Profile(), Profile(), Profile(), Profile()
        n_batches = len(self.dataloader)
        desc = self.get_desc()
        # NOTE: keeping `not self.training` in tqdm will eliminate pbar after segmentation evaluation during training,
        # which may affect classification task since this arg is in yolov5/classify/val.py.
        # bar = tqdm(self.dataloader, desc, n_batches, not self.training, bar_format=TQDM_BAR_FORMAT)
        bar = tqdm(self.dataloader, desc, n_batches, bar_format=TQDM_BAR_FORMAT)
        self.init_metrics(de_parallel(model))
        self.jdict = []  # empty before each val
        for batch_i, batch in enumerate(bar):
            self.batch_i = batch_i
            # pre-process
            with dt[0]:
                batch = self.preprocess(batch)

            # inference
            with dt[1]:
                preds = model(batch["img"])

            # loss
            with dt[2]:
                if self.training:
                    self.loss += trainer.criterion(preds, batch)[1]

            # pre-process predictions
            with dt[3]:
                preds = self.postprocess(preds)

            self.update_metrics(preds, batch)
            if self.args.plots and batch_i < 3:
                self.plot_val_samples(batch, batch_i)
                self.plot_predictions(batch, preds, batch_i)

        stats = self.get_stats()
        self.check_stats(stats)
        self.print_results()
        self.speed = tuple(x.t / len(self.dataloader.dataset) * 1E3 for x in dt)  # speeds per image
        if self.training:
            model.float()
            return {**stats, **trainer.label_loss_items(self.loss.cpu() / len(self.dataloader), prefix="val")}
        else:
            self.logger.info('Speed: %.1fms pre-process, %.1fms inference, %.1fms loss, %.1fms post-process per image' %
                             self.speed)
            if self.args.save_json and self.jdict:
                with open(str(self.save_dir / "predictions.json"), 'w') as f:
                    self.logger.info(f"Saving {f.name}...")
                    json.dump(self.jdict, f)  # flatten and save
                stats = self.eval_json(stats)  # update stats
            return stats

    def get_dataloader(self, dataset_path, batch_size):
        raise NotImplementedError("get_dataloader function not implemented for this validator")

    def preprocess(self, batch):
        return batch

    def postprocess(self, preds):
        return preds

    def init_metrics(self, model):
        pass

    def update_metrics(self, preds, batch):
        pass

    def get_stats(self):
        return {}

    def check_stats(self, stats):
        pass

    def print_results(self):
        pass

    def get_desc(self):
        pass

    @property
    def metric_keys(self):
        return []

    # TODO: may need to put these following functions into callback
    def plot_val_samples(self, batch, ni):
        pass

    def plot_predictions(self, batch, preds, ni):
        pass

    def pred_to_json(self, preds, batch):
        pass

    def eval_json(self, stats):
        pass