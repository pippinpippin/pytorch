from __future__ import absolute_import, division, print_function, unicode_literals
import torch
from torch.nn import Conv2d
from torch.nn import init
from torch.nn._intrinsic import ConvBn2d as NNConvBn2d
from torch.nn._intrinsic import ConvBnReLU2d as NNConvBnReLU2d
from torch.nn._intrinsic import ConvReLU2d as NNConvReLU2d
from ....qat.modules.conv import Conv2d as QATConv2d
from torch.nn import Parameter
import torch.nn.functional as F


class ConvBn2d(Conv2d):
    r"""
    A ConvBn2d module is a module fused from Conv2d and BatchNorm2d,
    attached with FakeQuantize modules for both output activation and weight,
    used in quantization aware training.

    We combined the interface of :class:`torch.nn.Conv2d` and
    :class:`torch.nn.BatchNorm2d`.

    Implementation details: https://arxiv.org/pdf/1806.08342.pdf section 3.2.2

    Similar to :class:`torch.nn.Conv2d`, with FakeQuantize modules initialized
    to default.

    Attributes:
        freeze_bn:
        observer: fake quant module for output activation, it's called observer
            to align with post training flow
        weight_fake_quant: fake quant module for weight

    """
    __FLOAT_MODULE = NNConvBn2d

    def __init__(self,
                 # Conv2d args
                 in_channels, out_channels, kernel_size, stride=1,
                 padding=0, dilation=1, groups=1,
                 # bias: None, only support Conv with no bias
                 padding_mode='zeros',
                 # BatchNorm2d args
                 # num_features: out_channels
                 eps=1e-05, momentum=0.1,
                 # affine: True
                 # track_running_stats: True
                 # Args for this module
                 freeze_bn=False,
                 activation_fake_quant=None,
                 weight_fake_quant=None):
        super(ConvBn2d, self).__init__(in_channels, out_channels, kernel_size,
                                       stride, padding, dilation, groups, False, padding_mode)
        self.eps = eps
        self.momentum = momentum
        self.freeze_bn = freeze_bn if self.training else True
        self.num_features = out_channels
        self.gamma = Parameter(torch.Tensor(out_channels))
        self.beta = Parameter(torch.Tensor(out_channels))
        self.affine = True
        self.track_running_stats = True
        self.register_buffer('running_mean', torch.zeros(out_channels))
        self.register_buffer('running_var', torch.ones(out_channels))
        self.register_buffer('num_batches_tracked', torch.tensor(0, dtype=torch.long))
        self.observer = activation_fake_quant()
        self.weight_fake_quant = weight_fake_quant()
        self.reset_bn_parameters()

    def reset_running_stats(self):
        self.running_mean.zero_()
        self.running_var.fill_(1)
        self.num_batches_tracked.zero_()

    def reset_bn_parameters(self):
        self.reset_running_stats()
        init.uniform_(self.gamma)
        init.zeros_(self.beta)

    def reset_parameters(self):
        super(ConvBn2d, self).reset_parameters()
        # A hack to avoid resetting on undefined parameters
        if hasattr(self, 'gamma'):
            self.reset_bn_parameters()

    def enable_fake_quant(self):
        self.observer.enable()
        self.weight_fake_quant.enable()
        return self

    def disable_fake_quant(self):
        self.observer.disable()
        self.weight_fake_quant.disable()
        return self

    def _forward(self, input):
        # exponential_average_factor is self.momentum set to
        # (when it is available) only so that if gets updated
        # in ONNX graph when this node is exported to ONNX.
        if self.momentum is None:
            exponential_average_factor = 0.0
        else:
            exponential_average_factor = self.momentum

        if self.training and self.track_running_stats:
            # TODO: if statement only here to tell the jit to skip emitting this when it is None
            if self.num_batches_tracked is not None:
                self.num_batches_tracked += 1
                if self.momentum is None:  # use cumulative moving average
                    exponential_average_factor = 1.0 / float(self.num_batches_tracked)
                else:  # use exponential moving average
                    exponential_average_factor = self.momentum

        # we use running statistics from the previous batch, so this is an
        # approximation of the approach mentioned in the whitepaper, but we only
        # need to do one convolution in this case instead of two
        running_std = torch.sqrt(self.running_var + self.eps)
        scale_factor = self.gamma / running_std
        scaled_weight = self.weight * scale_factor.reshape([-1, 1, 1, 1])
        conv = self.conv2d_forward(input, self.weight_fake_quant(scaled_weight))

        if self.training and not self.freeze_bn:
            # recovering original conv to get original batch_mean and batch_var
            conv_orig = conv / scale_factor.reshape([1, -1, 1, 1])
            batch_mean = torch.mean(conv_orig, dim=[0, 2, 3])
            batch_var = torch.var(conv_orig, dim=[0, 2, 3], unbiased=False)
            batch_rstd = torch.ones_like(batch_var) / torch.sqrt(batch_var + self.eps)

            rescale_factor = running_std * batch_rstd
            conv = conv * rescale_factor.reshape([1, -1, 1, 1])
            conv = conv + (self.beta - self.gamma * batch_mean * batch_rstd).reshape([1, -1, 1, 1])

            self.running_mean = exponential_average_factor * batch_mean + (1 - exponential_average_factor) * self.running_mean
            self.running_var = exponential_average_factor * batch_var + (1 - exponential_average_factor) * self.running_var
        else:
            conv = conv + (self.beta - self.gamma * self.running_mean /
                           running_std).reshape([1, -1, 1, 1])
        return conv

    def extra_repr(self):
        # TODO(jerryzh): extend
        return super(ConvBn2d, self).extra_repr()

    def forward(self, input):
        return self.observer(self._forward(input))

    @classmethod
    def from_float(cls, mod, qconfig):
        r"""Create a qat module from a float module or qparams_dict

            Args: `mod` a float module, either produced by torch.quantization utilities
            or directly from user
        """
        assert type(mod) == cls.__FLOAT_MODULE, ' qat.' + cls.__name__ + '.from_float only works for ' + \
            cls.__FLOAT_MODULE.__name__
        if not qconfig:
            assert hasattr(mod, 'qconfig'), 'Input float module must have qconfig defined'
            assert hasattr(mod, 'observer'), 'Input float module must have observer attached'
            qconfig = mod.qconfig
        conv, bn = mod[0], mod[1]
        qat_convbn = cls(conv.in_channels, conv.out_channels, conv.kernel_size,
                         conv.stride, conv.padding, conv.dilation,
                         conv.groups,
                         conv.padding_mode,
                         bn.eps, bn.momentum,
                         False,
                         qconfig.activation,
                         qconfig.weight)
        assert qat_convbn.bias is None, 'QAT ConvBn should not have bias'
        qat_convbn.weight = conv.weight
        qat_convbn.gamma = bn.weight
        qat_convbn.beta = bn.bias
        qat_convbn.running_mean = bn.running_mean
        qat_convbn.running_var = bn.running_var
        qat_convbn.num_batches_tracked = bn.num_batches_tracked
        return qat_convbn

class ConvBnReLU2d(ConvBn2d):
    r"""
    A ConvBn2d module is a module fused from Conv2d, BatchNorm2d and ReLU,
    attached with FakeQuantize modules for both output activation and weight,
    used in quantization aware training.

    We combined the interface of :class:`torch.nn.Conv2d` and
    :class:`torch.nn.BatchNorm2d` and :class:`torch.nn.ReLU`.

    Implementation details: https://arxiv.org/pdf/1806.08342.pdf

    Similar to `torch.nn.Conv2d`, with FakeQuantize modules initialized to
    default.

    Attributes:
        observer: fake quant module for output activation, it's called observer
            to align with post training flow
        weight_fake_quant: fake quant module for weight

    """
    __FLOAT_MODULE = NNConvBnReLU2d

    def __init__(self,
                 # Conv2d args
                 in_channels, out_channels, kernel_size, stride=1,
                 padding=0, dilation=1, groups=1,
                 # bias: None, only support Conv with no bias
                 padding_mode='zeros',
                 # BatchNorm2d args
                 # num_features: out_channels
                 eps=1e-05, momentum=0.1,
                 # affine: True
                 # track_running_stats: True
                 # Args for this module
                 freeze_bn=False,
                 activation_fake_quant=None,
                 weight_fake_quant=None):
        super(ConvBnReLU2d, self).__init__(in_channels, out_channels, kernel_size, stride,
                                           padding, dilation, groups,
                                           padding_mode, eps, momentum,
                                           freeze_bn,
                                           activation_fake_quant,
                                           weight_fake_quant)

    def forward(self, input):
        return self.observer(F.relu(super(ConvBnReLU2d, self)._forward(input)))

class ConvReLU2d(QATConv2d):
    r"""
    A ConvReLU2d module is a fused module of Conv2d and ReLU, attached with
    FakeQuantize modules for both output activation and weight for
    quantization aware training.

    We combined the interface of :class:`~torch.nn.Conv2d` and
    :class:`~torch.nn.BatchNorm2d`.

    Attributes:
        observer: fake quant module for output activation, it's called observer
            to align with post training flow
        weight_fake_quant: fake quant module for weight

    """
    __FLOAT_MODULE = NNConvReLU2d

    def __init__(self, in_channels, out_channels, kernel_size, stride=1,
                 padding=0, dilation=1, groups=1,
                 bias=True, padding_mode='zeros',
                 activation_fake_quant=None,
                 weight_fake_quant=None):
        super(ConvReLU2d, self).__init__(in_channels, out_channels, kernel_size,
                                         stride=stride, padding=padding, dilation=dilation,
                                         groups=groups, bias=bias, padding_mode=padding_mode)
        self.observer = activation_fake_quant()
        self.weight_fake_quant = weight_fake_quant()

    def forward(self, input):
        return self.observer(F.relu(conv2d_forward(input, self.padding_mode,
                             self.padding, self.weight_fake_quant(self.weight),
                             self.bias, self.stride, self.dilation, self.groups),
                             True))
