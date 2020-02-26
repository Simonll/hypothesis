import hypothesis
import numpy as np
import torch

from hypothesis.simulation import Simulator as BaseSimulator
from torch.distributions.binomial import Binomial



class SIRSimulator(BaseSimulator):

    step_size = 0.01
    N = 1000

    def __init__(self, default_measurement_time=1.0):
        super(SIRSimulator, self).__init__()
        self.default_measurement_time = default_measurement_time

    def simulate(self, theta, psi):
        # theta = [beta, gamma]
        # psi = tau
        # sample = [S(tau), I(tau), R(tau)]
        beta = theta[0].item()
        gamma = theta[1].item()
        S = self.N - 1
        I = 1
        R = 0
        n_steps = int(psi / self.step_size)
        for i in range(n_steps):
            delta_I = int(Binomial(S, beta * I / self.N).sample())
            delta_R = int(Binomial(I, gamma).sample())
            S -= delta_I
            I = I + delta_I - delta_R
            R += delta_R

        return torch.tensor([S, I, R]).float()

    @torch.no_grad()
    def forward(self, inputs, experimental_configurations=None):
        outputs = []

        n = len(inputs)
        for index in range(n):
            theta = inputs[index]
            if experimental_configurations is not None:
                psi = experimental_configurations[index]
                x = self.simulate(theta, psi.item())
            else:
                x = self.simulate(theta, self.default_measurement_time)
            outputs.append(x.view(1, -1))
        outputs = torch.cat(outputs, dim=0)

        return outputs