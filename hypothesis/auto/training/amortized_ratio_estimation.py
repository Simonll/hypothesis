import hypothesis
import numpy as np
import os
import re
import torch

from .base import BaseTrainer
from hypothesis.nn.amortized_ratio_estimation import BaseCriterion
from hypothesis.nn.amortized_ratio_estimation import ConservativeLikelihoodToEvidenceCriterion
from hypothesis.nn.amortized_ratio_estimation import LikelihoodToEvidenceCriterion
from hypothesis.summary import TrainingSummary as Summary



class BaseAmortizedRatioEstimatorTrainer(BaseTrainer):

    def __init__(self,
        criterion,
        estimator,
        feeder,
        optimizer,
        dataset_train,
        accelerator=hypothesis.accelerator,
        batch_size=hypothesis.default.batch_size,
        checkpoint=None,
        dataset_test=None,
        epochs=hypothesis.default.epochs,
        identifier=None,
        lr_scheduler_epoch=None,
        lr_scheduler_update=None,
        shuffle=True,
        workers=hypothesis.default.dataloader_workers):
        super(BaseAmortizedRatioEstimatorTrainer, self).__init__(
            batch_size=batch_size,
            checkpoint=checkpoint,
            epochs=epochs,
            identifier=identifier,
            shuffle=shuffle,
            workers=workers)
        # Datasets
        self.dataset_train = dataset_train
        self.dataset_test = dataset_test
        # Trainer state
        self.accelerator = accelerator
        self.criterion = criterion
        self.current_epoch = 0
        self.epochs_remaining = self.epochs
        self.estimator = estimator
        self.feeder = feeder
        self.losses_test = []
        self.losses_train = []
        self.lr_scheduler_epoch = lr_scheduler_epoch
        self.lr_scheduler_update = lr_scheduler_update
        self.optimizer = optimizer
        self.best_epoch = None
        self.best_loss = float("infinity")
        self.best_model = None
        # Move estimator and criterion to the specified accelerator.
        self.estimator = self.estimator.to(self.accelerator)
        self.criterion = self.criterion.to(self.accelerator)

    def _register_events(self):
        self.register_event("batch_complete")
        self.register_event("batch_start")
        self.register_event("epoch_complete")
        self.register_event("epoch_start")

    def _valid_checkpoint_path(self):
        return self.checkpoint_path is not None and len(self.checkpoint_path) > 0

    def _valid_checkpoint_path_and_exists(self):
        return self._valid_checkpoint_path() and os.path.exists(self.checkpoint_path)

    @torch.no_grad()
    def _checkpoint_store(self):
        if self._valid_checkpoint_path():
            state = {}
            state["accelerator"] = self.accelerator
            state["current_epoch"] = self.current_epoch
            state["estimator"] = self._cpu_estimator_state_dict()
            state["epochs_remaining"] = self.epochs_remaining
            state["epochs"] = self.epochs
            state["losses_test"] = self.losses_test
            state["losses_train"] = self.losses_train
            if self.lr_scheduler_update is not None:
                state["lr_scheduler_update"] = self.lr_scheduler_update.state_dict()
            if self.lr_scheduler_epoch is not None:
                state["lr_scheduler_epoch"] = self.lr_scheduler_epoch.state_dict()
            state["optimizer"] = self.optimizer.state_dict()
            state["best_epoch"] = self.best_epoch
            state["best_loss"] = self.best_loss
            state["best_model"] = self.best_model
            torch.save(state, self.checkpoint_path)

    def _checkpoint_load(self):
        if self._valid_checkpoint_path_and_exists():
            raise NotImplementedError

    def _summarize(self):
        return Summary(
            identifier=self.identifier,
            model_best=self.best_model,
            model_final=self._cpu_estimator_state_dict(),
            epoch_best=self.best_epoch,
            epochs=self.epochs,
            losses_train=np.array(self.losses_train).reshape(-1),
            losses_test=np.array(self.losses_test).reshape(-1))

    @torch.no_grad()
    def _cpu_estimator_state_dict(self):
        # Check if we're training a Data Parallel model.
        self.estimator = self.estimator.cpu()
        if isinstance(self.estimator, torch.nn.DataParallel):
            state_dict = self.estimator.module.state_dict()
        else:
            state_dict = self.estimator.state_dict()
        self.estimator = self.estimator.to(hypothesis.accelerator)

        return state_dict

    @torch.no_grad()
    def checkpoint(self):
        self._checkpoint_store()

    def fit(self):
        # Training procedure
        for epoch in range(self.epochs):
            self.current_epoch = epoch + 1
            self.call_event(self.events.epoch_start)
            self.train()
            # Check if a testing dataset is available.
            if self.dataset_test is not None and len(self.dataset_test) > 0:
                loss = self.test()
            else:
                self.best_model = self._cpu_estimator_state_dict()
            # Check if a learning rate scheduler has been allocated.
            if self.lr_scheduler_epoch is not None:
                self.lr_scheduler_epoch.step(loss)
            self.epochs_remaining -= 1
            self.checkpoint()
            self.call_event(self.events.epoch_complete)
        # Remove the checkpoint.
        if self._valid_checkpoint_path_and_exists():
            os.remove(self.checkpoint_path)

        return self._summarize()

    @torch.no_grad()
    def test(self):
        self.estimator.eval()
        loader = self._allocate_data_loader(self.dataset_test)
        total_loss = 0.0
        for batch in loader:
            loss = self.feeder(
                accelerator=self.accelerator,
                batch=batch,
                criterion=self.criterion)
            total_loss += loss.item()
        total_loss /= len(loader)
        self.losses_test.append(total_loss)
        if total_loss < self.best_loss:
            state_dict = self._cpu_estimator_state_dict()
            self.best_loss = total_loss
            self.best_model = state_dict
            self.best_epoch = self.current_epoch

        return total_loss

    def train(self):
        self.estimator.train()
        loader = self._allocate_data_loader(self.dataset_train)
        for index, batch in enumerate(loader):
            self.call_event(self.events.batch_start)
            loss = self.feeder(
                accelerator=self.accelerator,
                batch=batch,
                criterion=self.criterion)
            self.optimizer.zero_grad()
            loss.backward()
            self.optimizer.step()
            if self.lr_scheduler_update is not None:
                self.lr_scheduler_update.step()
            loss = loss.item()
            self.losses_train.append(loss)
            self.call_event(self.events.batch_complete, index=index, loss=loss)



class LikelihoodToEvidenceRatioEstimatorTrainer(BaseAmortizedRatioEstimatorTrainer):

    def __init__(self,
        estimator,
        optimizer,
        dataset_train,
        accelerator=hypothesis.accelerator,
        batch_size=hypothesis.default.batch_size,
        criterion=None,
        checkpoint=None,
        dataset_test=None,
        epochs=hypothesis.default.epochs,
        identifier=None,
        lr_scheduler_epoch=None,
        lr_scheduler_update=None,
        workers=hypothesis.default.dataloader_workers):
        if criterion is None:
            criterion = LikelihoodToEvidenceCriterion(
                batch_size=batch_size,
                estimator=estimator)
        feeder = LikelihoodToEvidenceRatioEstimatorTrainer.feeder
        super(LikelihoodToEvidenceRatioEstimatorTrainer, self).__init__(
            accelerator=accelerator,
            batch_size=batch_size,
            checkpoint=checkpoint,
            criterion=criterion,
            dataset_test=dataset_test,
            dataset_train=dataset_train,
            epochs=epochs,
            estimator=estimator,
            feeder=feeder,
            identifier=identifier,
            lr_scheduler_epoch=lr_scheduler_epoch,
            lr_scheduler_update=lr_scheduler_update,
            optimizer=optimizer,
            workers=workers)

    @staticmethod
    def feeder(batch, criterion, accelerator):
        inputs, outputs = batch
        inputs = inputs.to(accelerator, non_blocking=True)
        outputs = outputs.to(accelerator, non_blocking=True)

        return criterion(inputs=inputs, outputs=outputs)



def create_trainer(criterion, denominator):
    r"""Variables of the dataset must by sorted by variable name."""
    variables = re.split(",|\|", denominator)
    variables.sort()

    assert(set(variables) == set(criterion.variables()))

    # Allocate the batch-feeder based on the specified random variables.
    def feeder(batch, criterion, accelerator):
        dictionary = {}
        for index, key in enumerate(variables):
            dictionary[key] = batch[index].to(accelerator, non_blocking=True)

        return criterion(**dictionary)

    # Create the trainer object with the desired criterion.
    class Trainer(BaseAmortizedRatioEstimatorTrainer):

        def __init__(self,
            estimator,
            optimizer,
            dataset_train,
            criterion,
            accelerator=hypothesis.accelerator,
            batch_size=hypothesis.default.batch_size,
            checkpoint=None,
            dataset_test=None,
            epochs=hypothesis.default.epochs,
            lr_scheduler=None,
            identifier=None,
            shuffle=True,
            workers=hypothesis.default.dataloader_workers):
            super(Trainer, self).__init__(
                accelerator=accelerator,
                batch_size=batch_size,
                checkpoint=checkpoint,
                criterion=criterion,
                dataset_test=dataset_test,
                dataset_train=dataset_train,
                epochs=epochs,
                estimator=estimator,
                feeder=feeder,
                identifier=identifier,
                lr_scheduler_epoch=lr_scheduler,
                optimizer=optimizer,
                shuffle=shuffle,
                workers=workers)

    return Trainer
