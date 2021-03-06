import hypothesis
import hypothesis.nn
import torch

from hypothesis.nn import MultiLayeredPerceptron as MLP
from hypothesis.nn import ResNetHead
from hypothesis.nn.amortized_ratio_estimation import BaseRatioEstimator
from hypothesis.nn.resnet.default import batchnorm as default_batchnorm
from hypothesis.nn.resnet.default import channels as default_channels
from hypothesis.nn.resnet.default import convolution_bias as default_convolution_bias
from hypothesis.nn.resnet.default import depth as default_depth
from hypothesis.nn.resnet.default import dilate as default_dilate
from hypothesis.nn.resnet.default import groups as default_groups
from hypothesis.nn.resnet.default import in_planes as default_in_planes
from hypothesis.nn.resnet.default import width_per_group as default_width_per_group
from hypothesis.nn.util import compute_dimensionality



def build_ratio_estimator(random_variables, **kwargs):
    depth = kwargs.get("depth", default_depth)
    convolve_variable = kwargs.get("convolve", "outputs")
    trunk_variables = set(random_variables.keys()) - set([convolve_variable])
    trunk_random_variables = {}
    for k in trunk_variables:
        trunk_random_variables[k] = (-1, compute_dimensionality(random_variables[k]))
    if convolve_variable not in random_variables.keys():
        raise ValueError("No convolution random variable specified (default: outputs)!")

    class RatioEstimator(BaseRatioEstimator):

        def __init__(self,
            activation=hypothesis.default.activation,
            batchnorm=default_batchnorm,
            channels=default_channels,
            convolution_bias=default_convolution_bias,
            depth=depth,
            dilate=default_dilate,
            groups=default_groups,
            in_planes=default_in_planes,
            trunk_activation=None,
            trunk_dropout=hypothesis.default.dropout,
            trunk_layers=hypothesis.default.trunk,
            width_per_group=default_width_per_group):
            super(RatioEstimator, self).__init__()
            # Construct the convolutional ResNet head.
            self.head = ResNetHead(
                activation=hypothesis.default.activation,
                batchnorm=batchnorm,
                channels=channels,
                convolution_bias=convolution_bias,
                depth=depth,
                dilate=dilate,
                groups=groups,
                in_planes=in_planes,
                shape_xs=random_variables[convolve_variable],
                width_per_group=width_per_group)
            # Check if custom trunk settings have been defined.
            if trunk_activation is None:
                trunk_activation = activation
            # Construct the trunk of the network.
            self.embedding_dimensionality = self.head.embedding_dimensionality()
            dimensionality = self.embedding_dimensionality + sum([compute_dimensionality(random_variables[k]) for k in trunk_random_variables])
            self.trunk = MLP(
                shape_xs=(dimensionality,),
                shape_ys=(1,),
                activation=trunk_activation,
                dropout=trunk_dropout,
                layers=trunk_layers,
                transform_output=None)

        def log_ratio(self, **kwargs):
            z_head = self.head(kwargs[convolve_variable]).view(-1, self.embedding_dimensionality)
            tensors = [kwargs[k].view(v) for k, v in trunk_random_variables.items()]
            tensors.append(z_head)
            features = torch.cat(tensors, dim=1)
            log_ratios = self.trunk(features)

            return log_ratios

    return RatioEstimator
