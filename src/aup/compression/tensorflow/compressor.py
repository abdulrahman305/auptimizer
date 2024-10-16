# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.

"""
Abstract base classes for TensorFlow model compression.
"""

import logging
_logger = logging.getLogger(__name__)

try:
    import tensorflow as tf
except (ImportError):
    _logger.debug("Could not import Tensorflow")

try:
    assert tf.__version__.startswith('2'), 'NNI model compression only supports TensorFlow v2.x'
except (AssertionError):
    _logger.debug("Could not import Tensorflow 2.X. Install Tensorflow 2.X for NNI model compression.")


from . import default_layers


class Compressor:
    """
    Common base class for all compressors.

    This class is designed for other base classes.
    Algorithms should inherit ``Pruner`` or ``Quantizer`` instead.

    Attributes
    ----------
    compressed_model : tf.keras.Model
        Compressed user model.
    wrappers : list of tf.keras.Model
        A wrapper is an instrumented TF ``Layer``, in ``Model`` format.

    Parameters
    ----------
    model : tf.keras.Model
        The user model to be compressed.
    config_list : list of JSON object
        User configuration. The format is detailed in tutorial.
    LayerWrapperClass : a class derive from Model
        The class used to instrument layers.
    """

    def __init__(self, model, config_list, LayerWrapperClass):
        assert isinstance(model, tf.keras.Model)
        self.validate_config(model, config_list)

        self._original_model = model
        self._config_list = config_list
        self._wrapper_class = LayerWrapperClass
        self._wrappers = {}  # key: id(layer) , value: Wrapper(layer)

        self.compressed_model = self._instrument(model)
        self.wrappers = list(self._wrappers.values())

        if not self.wrappers:
            _logger.warning('Nothing is configured to compress, please check your model and config list')

    def set_wrappers_attribute(self, name, value):
        """
        Call ``setattr`` on all wrappers.
        """
        for wrapper in self.wrappers:
            setattr(wrapper, name, value)

    def validate_config(self, model, config_list):
        """
        Compression algorithm should overload this function to validate configuration.
        """
        pass


    def _instrument(self, layer):
        if isinstance(layer, tf.keras.Sequential):
            return self._instrument_sequential(layer)
        if isinstance(layer, tf.keras.Model):
            return self._instrument_model(layer)

        # a layer can be referenced in multiple attributes of a model,
        # but should only be instrumented once
        if id(layer) in self._wrappers:
            return self._wrappers[id(layer)]

        config = self._select_config(layer)
        if config is not None:
            wrapper = self._wrapper_class(layer, config, self)
            self._wrappers[id(layer)] = wrapper
            return wrapper

        return layer

    def _instrument_sequential(self, seq):
        layers = list(seq.layers)  # seq.layers is read-only property
        need_rebuild = False
        for i, layer in enumerate(layers):
            new_layer = self._instrument(layer)
            if new_layer is not layer:
                layers[i] = new_layer
                need_rebuild = True
        return tf.keras.Sequential(layers) if need_rebuild else seq

    def _instrument_model(self, model):
        for key, value in list(model.__dict__.items()):  # avoid "dictionary keys changed during iteration"
            if isinstance(value, tf.keras.layers.Layer):
                new_layer = self._instrument(value)
                if new_layer is not value:
                    setattr(model, key, new_layer)
            elif isinstance(value, list):
                for i, item in enumerate(value):
                    if isinstance(item, tf.keras.layers.Layer):
                        value[i] = self._instrument(item)
        return model


    def _select_config(self, layer):
        # Find the last matching config block for given layer.
        # Returns None if the layer should not be compressed.
        layer_type = type(layer).__name__
        last_match = None
        for config in self._config_list:
            if 'op_types' in config:
                match = layer_type in config['op_types']
                match_default = 'default' in config['op_types'] and layer_type in default_layers.weighted_modules
                if not match and not match_default:
                    continue
            if 'op_names' in config and layer.name not in config['op_names']:
                continue
            last_match = config
        if last_match is None or 'exclude' in last_match:
            return None
        return last_match


class Pruner(Compressor):
    """
    Base class for pruning algorithms.

    End users should use ``compress`` and callback APIs (WIP) to prune their models.

    The underlying model is instrumented upon initialization of pruner object.
    So if you want to pre-train the model, train it before creating pruner object.

    The compressed model can only execute in eager mode.

    Algorithm developers should override ``calc_masks`` method to specify pruning strategy.

    Parameters
    ----------
    model : tf.keras.Model
        The user model to prune.
    config_list : list of JSON object
        User configuration. The format is detailed in tutorial.
    """
    def __init__(self, model, config_list):
        super().__init__(model, config_list, PrunerLayerWrapper)
        #self.callback = PrunerCallback(self)

    def compress(self):
        """
        Apply compression on a pre-trained model.

        If you want to prune the model during training, use callback API (WIP) instead.

        Returns
        -------
        tf.keras.Model
            The compressed model.
        """
        self._update_mask()
        return self.compressed_model

    def calc_masks(self, wrapper, **kwargs):
        """
        Abstract method to be overridden by algorithm. End users should ignore it.

        If the callback is set up, this method will be invoked at end of each training minibatch.
        If not, it will only be called when end user invokes ``compress``.

        Parameters
        ----------
        wrapper : PrunerLayerWrapper
            The instrumented layer.
        **kwargs
            Reserved for forward compatibility.

        Returns
        -------
        dict of (str, tf.Tensor), or None
            The key is weight ``Variable``'s name. The value is a mask ``Tensor`` of weight's shape and dtype.
            If a weight's key does not appear in the return value, that weight will not be pruned.
            Returning ``None`` means the mask is not changed since last time.
            Weight names are globally unique, e.g. `model/conv_1/kernel:0`.
        """
        # TODO: maybe it should be able to calc on weight-granularity, beside from layer-granularity
        raise NotImplementedError("Pruners must overload calc_masks()")

    def _update_mask(self):
        for wrapper_idx, wrapper in enumerate(self.wrappers):
            masks = self.calc_masks(wrapper, wrapper_idx=wrapper_idx)
            if masks is not None:
                wrapper.masks = masks


class PrunerLayerWrapper(tf.keras.Model):
    """
    Instrumented TF layer.

    Wrappers will be passed to pruner's ``calc_masks`` API,
    and the pruning algorithm should use wrapper's attributes to calculate masks.

    Once instrumented, underlying layer's weights will get **modified** by masks before forward pass.

    Attributes
    ----------
    layer_info : LayerInfo
        All static information of the original layer.
    layer : tf.keras.layers.Layer
        The original layer.
    config : JSON object
        Selected configuration. The format is detailed in tutorial.
    pruner : Pruner
        Bound pruner object.
    masks : dict of (str, tf.Tensor)
        Current masks. The key is weight's name and the value is mask tensor.
        On initialization, `masks` is an empty dict, which means no weight is pruned.
        Afterwards, `masks` is the last return value of ``Pruner.calc_masks``.
        See ``Pruner.calc_masks`` for details.
    """
    def __init__(self, layer, config, pruner):
        super().__init__()
        self.layer = layer
        self.config = config
        self.pruner = pruner
        self.masks = {}
        _logger.info('Layer detected to compress: %s', self.layer.name)

    def call(self, *inputs):
        new_weights = []
        for weight in self.layer.weights:
            mask = self.masks.get(weight.name)
            if mask is not None:
                new_weights.append(tf.math.multiply(weight, mask))
            else:
                new_weights.append(weight)
        if new_weights and not hasattr(new_weights[0], 'numpy'):
            raise RuntimeError('NNI: Compressed model can only run in eager mode')
        self.layer.set_weights([weight.numpy() for weight in new_weights])
        return self.layer(*inputs)


# TODO: designed to replace `patch_optimizer`
#class PrunerCallback(tf.keras.callbacks.Callback):
#    def __init__(self, pruner):
#        super().__init__()
#        self._pruner = pruner
#
#    def on_train_batch_end(self, batch, logs=None):
#        self._pruner.update_mask()
