"""
Temporal Convolutional Network
------------------------------
"""

import math
import torch.nn as nn
import torch.nn.functional as F
from numpy.random import RandomState
from typing import Optional, Union
from ..utils.torch import random_method
from ..utils.data.shifted_dataset import ShiftedDataset

from ..logging import raise_if_not, get_logger
from .torch_forecasting_model import TorchForecastingModel  # , _TimeSeriesShiftedDataset

logger = get_logger(__name__)


class _ResidualBlock(nn.Module):

    def __init__(self,
                 num_filters: int,
                 kernel_size: int,
                 dilation_base: int,
                 dropout: float,
                 weight_norm: bool,
                 nr_blocks_below: int,
                 num_layers: int,
                 input_size: int,
                 target_size: int):
        """ PyTorch module implementing a residual block module used in `_TCNModule`.

        Parameters
        ----------
        num_filters
            The number of filters in a convolutional layer of the TCN.
        kernel_size
            The size of every kernel in a convolutional layer.
        dilation_base
            The base of the exponent that will determine the dilation on every level.
        dropout
            The dropout rate for every convolutional layer.
        weight_norm
            Boolean value indicating whether to use weight normalization.
        nr_blocks_below
            The number of residual blocks before the current one.
        num_layers
            The number of convolutional layers.
        input_size
            The dimensionality of the input time series of the whole network.
        target_size
            The dimensionality of the output time series of the whole network.

        Inputs
        ------
        x of shape `(batch_size, in_dimension, input_length)`
            Tensor containing the features of the input sequence.
            in_dimension is equal to `input_size` if this is the first residual block,
            in all other cases it is equal to `num_filters`.

        Outputs
        -------
        y of shape `(batch_size, out_dimension, input_length)`
            Tensor containing the output sequence of the residual block.
            out_dimension is equal to `target_size` if this is the last residual block,
            in all other cases it is equal to `num_filters`.
        """
        super(_ResidualBlock, self).__init__()

        self.dilation_base = dilation_base
        self.kernel_size = kernel_size
        self.dropout = dropout
        self.num_layers = num_layers
        self.nr_blocks_below = nr_blocks_below

        input_dim = input_size if nr_blocks_below == 0 else num_filters
        output_dim = target_size if nr_blocks_below == num_layers - 1 else num_filters
        self.conv1 = nn.Conv1d(input_dim, num_filters, kernel_size, dilation=(dilation_base ** nr_blocks_below))
        self.conv2 = nn.Conv1d(num_filters, output_dim, kernel_size, dilation=(dilation_base ** nr_blocks_below))
        if weight_norm:
            self.conv1, self.conv2 = nn.utils.weight_norm(self.conv1), nn.utils.weight_norm(self.conv2)

        if nr_blocks_below == 0 or nr_blocks_below == num_layers - 1:
            self.conv3 = nn.Conv1d(input_dim, output_dim, 1)

    def forward(self, x):
        residual = x

        # first step
        left_padding = (self.dilation_base ** self.nr_blocks_below) * (self.kernel_size - 1)
        x = F.pad(x, (left_padding, 0))
        x = self.dropout(F.relu(self.conv1(x)))

        # second step
        x = F.pad(x, (left_padding, 0))
        if self.nr_blocks_below < self.num_layers - 1:
            x = F.relu(x)
        x = self.dropout((self.conv2(x)))

        # add residual
        if self.nr_blocks_below in {0, self.num_layers - 1}:
            residual = self.conv3(residual)
        x += residual

        return x


class _TCNModule(nn.Module):
    def __init__(self,
                 input_size: int,
                 input_length: int,
                 kernel_size: int,
                 num_filters: int,
                 num_layers: Optional[int],
                 dilation_base: int,
                 weight_norm: bool,
                 target_size: int,
                 target_length: int,
                 dropout: float):

        """ PyTorch module implementing a dilated TCN module used in `TCNModel`.


        Parameters
        ----------
        input_size
            The dimensionality of the input time series.
        target_size
            The dimensionality of the output time series.
        input_length
            The length of the input time series.
        target_length
            Number of time steps the torch module will predict into the future at once.
        kernel_size
            The size of every kernel in a convolutional layer.
        num_filters
            The number of filters in a convolutional layer of the TCN.
        num_layers
            The number of convolutional layers.
        weight_norm
            Boolean value indicating whether to use weight normalization.
        dilation_base
            The base of the exponent that will determine the dilation on every level.
        dropout
            The dropout rate for every convolutional layer.

        Inputs
        ------
        x of shape `(batch_size, input_length, input_size)`
            Tensor containing the features of the input sequence.

        Outputs
        -------
        y of shape `(batch_size, input_length, 1)`
            Tensor containing the predictions of the next 'target_length' points in the last
            'target_length' entries of the tensor. The entries before contain the data points
            leading up to the first prediction, all in chronological order.
        """

        super(_TCNModule, self).__init__()

        # Defining parameters
        self.input_size = input_size
        self.input_length = input_length
        self.n_filters = num_filters
        self.kernel_size = kernel_size
        self.target_length = target_length
        self.target_size = target_size
        self.dilation_base = dilation_base
        self.dropout = nn.Dropout(p=dropout)

        # If num_layers is not passed, compute number of layers needed for full history coverage
        if num_layers is None and dilation_base > 1:
            num_layers = math.ceil(math.log((input_length - 1) * (dilation_base - 1) / (kernel_size - 1) / 2 + 1,
                                            dilation_base))
            logger.info("Number of layers chosen: " + str(num_layers))
        elif num_layers is None:
            num_layers = math.ceil((input_length - 1) / (kernel_size - 1) / 2)
            logger.info("Number of layers chosen: " + str(num_layers))
        self.num_layers = num_layers

        # Building TCN module
        self.res_blocks_list = []
        for i in range(num_layers):
            res_block = _ResidualBlock(num_filters, kernel_size, dilation_base,
                                       self.dropout, weight_norm, i, num_layers, self.input_size, target_size)
            self.res_blocks_list.append(res_block)
        self.res_blocks = nn.ModuleList(self.res_blocks_list)

    def forward(self, x):
        # data is of size (batch_size, input_length, input_size)
        batch_size = x.size(0)
        x = x.transpose(1, 2)

        for res_block in self.res_blocks_list:
            x = res_block(x)

        x = x.transpose(1, 2)
        x = x.view(batch_size, self.input_length, self.target_size)

        return x


class TCNModel(TorchForecastingModel):

    @random_method
    def __init__(self,
                 input_length: int = 12,
                 input_size: int = 1,
                 target_length: int = 1,
                 target_size: int = 1,
                 kernel_size: int = 3,
                 num_filters: int = 3,
                 num_layers: Optional[int] = None,
                 dilation_base: int = 2,
                 weight_norm: bool = False,
                 dropout: float = 0.2,
                 random_state: Optional[Union[int, RandomState]] = None,
                 **kwargs):

        """ Temporal Convolutional Network Model (TCN).

        This is an implementation of a dilated TCN used for forecasting.
        Inspiration: https://arxiv.org/abs/1803.01271

        Parameters
        ----------
        input_length
            Number of past time steps that are fed to the forecasting module.
        input_size
            The dimensionality of the TimeSeries instances that will be fed to the fit function.
        target_length
            Number of time steps the torch module will predict into the future at once.
        target_size
            The dimensionality of the output time series.
        kernel_size
            The size of every kernel in a convolutional layer.
        num_filters
            The number of filters in a convolutional layer of the TCN.
        weight_norm
            Boolean value indicating whether to use weight normalization.
        dilation_base
            The base of the exponent that will determine the dilation on every level.
        num_layers
            The number of convolutional layers.
        dropout
            The dropout rate for every convolutional layer.
        random_state
            Control the randomness of the weights initialization. Check this
            `link <https://scikit-learn.org/stable/glossary.html#term-random-state>`_ for more details.
        """

        raise_if_not(kernel_size < input_length,
                     "The kernel size must be strictly smaller than the input length.", logger)
        raise_if_not(target_length < input_length,
                     "The output length must be strictly smaller than the input length", logger)

        kwargs['input_length'] = input_length
        kwargs['target_length'] = target_length
        kwargs['input_size'] = input_size
        kwargs['target_size'] = target_size

        self.model = _TCNModule(input_size=input_size, input_length=input_length, target_size=target_size,
                                kernel_size=kernel_size, num_filters=num_filters,
                                num_layers=num_layers, dilation_base=dilation_base,
                                target_length=target_length, dropout=dropout, weight_norm=weight_norm)

        super().__init__(**kwargs)

    def build_ts_dataset_from_single_series(self, series):
        return ShiftedDataset(series, seq_length=self.input_length, shift=self.target_length)

    @property
    def first_prediction_index(self) -> int:
        return -self.target_length
