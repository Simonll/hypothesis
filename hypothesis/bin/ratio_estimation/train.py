"""General training script fo Amortised Approximate Ratio Estimation (AARE)."""

import argparse
import hypothesis
import importlib
import numpy as np
import os
import torch

from hypothesis.auto.training import LikelihoodToEvidenceRatioEstimatorTrainer as Trainer
from hypothesis.auto.training import create_trainer
from hypothesis.nn.amortized_ratio_estimation import BaseConservativeCriterion
from hypothesis.nn.amortized_ratio_estimation import BaseCriterion
from hypothesis.nn.amortized_ratio_estimation import BaseExperimentalCriterion
from torch.optim.lr_scheduler import ReduceLROnPlateau
from torch.optim.lr_scheduler import StepLR
from torch.utils.data import TensorDataset
from tqdm import tqdm



def main(arguments):
    # Allocate the datasets
    dataset_test = allocate_dataset_test(arguments)
    dataset_train = allocate_dataset_train(arguments)
    # Allocate the ratio estimator
    estimator = allocate_estimator(arguments)
    # Check if the gradients have to be clipped.
    if arguments.clip_grad != 0.0:
        for p in estimator.parameters():
            p.register_hook(lambda grad: torch.clamp(grad, -arguments.clip_grad, arguments.clip_grad))
    # Allocate the optimizer
    optimizer = torch.optim.AdamW(
        estimator.parameters(),
        amsgrad=arguments.amsgrad,
        lr=arguments.lr,
        weight_decay=arguments.weight_decay)
    # Prepare the training criterion
    if arguments.conservativeness > 0.0:
        criterion = BaseConservativeCriterion(
            batch_size=arguments.batch_size,
            beta=arguments.conservativeness,
            denominator=arguments.denominator,
            estimator=estimator,
            logits=arguments.logits)
    else:
        criterion = BaseCriterion(
            batch_size=arguments.batch_size,
            denominator=arguments.denominator,
            estimator=estimator,
            logits=arguments.logits)
    # Check if the experimental settings have to be activated
    if arguments.experimental:
        criterion = BaseExperimentalCriterion(
            batch_size=arguments.batch_size,
            denominator=arguments.denominator,
            estimator=estimator,
            logits=arguments.logits)
    # Allocate the learning rate scheduler, if requested.
    if arguments.lrsched:
        if arguments.lrsched_every is None or arguments.lrsched_gamma is None:
            lr_scheduler = ReduceLROnPlateau(optimizer, verbose=True)
        else:
            lr_scheduler = StepLR(optimizer, step_size=arguments.lrsched_every, gamma=arguments.lrsched_gamma)
    else:
        lr_scheduler = None
    # Allocate the trainer
    Trainer = create_trainer(criterion, arguments.denominator)
    trainer = Trainer(
        accelerator=hypothesis.accelerator,
        batch_size=arguments.batch_size,
        criterion=criterion,
        dataset_test=dataset_test,
        dataset_train=dataset_train,
        epochs=arguments.epochs,
        estimator=estimator,
        lr_scheduler=lr_scheduler,
        shuffle=(not arguments.dont_shuffle),
        optimizer=optimizer,
        workers=arguments.workers)
    # Register the callbacks
    if arguments.show:
        # Callbacks
        progress_bar = tqdm(total=arguments.epochs)
        def report_test_loss(caller):
            trainer = caller
            current_epoch = trainer.current_epoch
            test_loss = trainer.losses_test[-1]
            progress_bar.set_description("Test loss %s" % test_loss)
            progress_bar.update(1)
        trainer.add_event_handler(trainer.events.epoch_complete, report_test_loss)
    # Run the optimization procedure
    summary = trainer.fit()
    if arguments.show:
        # Cleanup the progress bar
        progress_bar.close()
        print(summary)
    if arguments.out is None:
        return # No output directory has been specified, exit.
    # Create the directory if it does not exist.
    if not os.path.exists(arguments.out):
        os.mkdir(arguments.out)
    best_model_weights = summary.best_model()
    final_model_weights = summary.final_model()
    train_losses = summary.train_losses()
    test_losses = summary.test_losses()
    # Save the results.
    np.save(arguments.out + "/losses-train.npy", train_losses)
    np.save(arguments.out + "/losses-test.npy", test_losses)
    torch.save(best_model_weights, arguments.out + "/best-model.th")
    torch.save(final_model_weights, arguments.out + "/model.th")
    summary.save(arguments.out + "/result.summary")


@torch.no_grad()
def allocate_dataset_train(arguments):
    return load_class(arguments.data_train)()


@torch.no_grad()
def allocate_dataset_test(arguments):
    if arguments.data_test is not None:
        dataset = load_class(arguments.data_test)()
    else:
        dataset = None

    return dataset


@torch.no_grad()
def allocate_estimator(arguments):
    estimator = load_class(arguments.estimator)()
    # Check if we are able to allocate a data parallel model.
    if torch.cuda.device_count() > 1 and arguments.data_parallel:
        estimator = torch.nn.DataParallel(estimator)
    estimator = estimator.to(hypothesis.accelerator)

    return estimator


def load_class(full_classname):
    if full_classname is None:
        raise ValueError("The specified classname cannot be `None`.")
    module_name, class_name = full_classname.rsplit('.', 1)
    module = __import__(module_name, fromlist=[class_name])

    return getattr(module, class_name)


def parse_arguments():
    parser = argparse.ArgumentParser("Amortised Approximate Ratio Estimator training")
    # General settings
    parser.add_argument("--data-parallel", action="store_true", help="Enable data-parallel training if multiple GPU's are available (default: false).")
    parser.add_argument("--disable-gpu", action="store_true", help="Disable the usage of the GPU, not recommended. (default: false).")
    parser.add_argument("--out", type=str, default=None, help="Output directory (default: none).")
    parser.add_argument("--show", action="store_true", help="Show the progress and the final result (default: false).")
    parser.add_argument("--dont-shuffle", action="store_true", help="Disables shuffling of the batch loader (default: false).")
    parser.add_argument("--denominator", type=str, default="inputs|outputs", help="Random variables in the denominator and their (in)dependence relation (default: 'inputs|outputs').")
    # Optimization settings
    parser.add_argument("--amsgrad", action="store_true", help="Use AMSGRAD version of Adam (default: false).")
    parser.add_argument("--batch-size", type=int, default=64, help="Batch size (default: 64).")
    parser.add_argument("--conservativeness", type=float, default=0.0, help="Conservative term (default: 0.0).")
    parser.add_argument("--clip-grad", type=float, default=0.0, help="Value to clip the gradients with (default: 0.0 or no clipping).")
    parser.add_argument("--epochs", type=int, default=1, help="Number of epochs (default: 1).")
    parser.add_argument("--logits", action="store_true", help="Use the logit-trick for the minimization criterion (default: false).")
    parser.add_argument("--lr", type=float, default=0.001, help="Learning rate (default: 0.001).")
    parser.add_argument("--lrsched", action="store_true", help="Enable learning rate scheduling (default: false).")
    parser.add_argument("--lrsched-every", type=int, default=None, help="Schedule the learning rate every n epochs (default: none).")
    parser.add_argument("--lrsched-gamma", type=float, default=None, help="Learning rate scheduling stepsize (default: none).")
    parser.add_argument("--weight-decay", type=float, default=0.0, help="Weight decay (default: 0.0).")
    parser.add_argument("--workers", type=int, default=2, help="Number of concurrent data loaders (default: 2).")
    # Data settings
    parser.add_argument("--data-test", type=str, default=None, help="Full classname of the testing dataset (default: none, optional).")
    parser.add_argument("--data-train", type=str, default=None, help="Full classname of the training dataset (default: none).")
    # Ratio estimator settings
    parser.add_argument("--estimator", type=str, default=None, help="Full classname of the ratio estimator (default: none).")
    # Experimental settings
    parser.add_argument("--experimental", action="store_true", help="Enable experimental settings (default: false).")
    arguments, _ = parser.parse_known_args()

    return arguments


if __name__ == "__main__":
    arguments = parse_arguments()
    main(arguments)
