

import tensorflow as tf
import torch
import numpy
import types
import tempfile
from pytorch_to_returnn.pprint import pprint
from typing import Callable, Optional, Dict, Any
from returnn.tf.util.data import Data
from pytorch_to_returnn import torch as torch_returnn
from pytorch_to_returnn.import_wrapper import wrapped_import, wrapped_import_demo
from pytorch_to_returnn.import_wrapper.torch_wrappers.tensor import WrappedTorchTensor
from pytorch_to_returnn.naming import Naming


ModelFuncType = Callable[[Optional[Callable[[str], types.ModuleType]], torch.Tensor], torch.Tensor]


class Converter:
  """
  Example code for model func::

    def model_func(wrapped_import, inputs):

        if typing.TYPE_CHECKING or not wrapped_import:
            import torch
            from parallel_wavegan import models as pwg_models
            from parallel_wavegan import layers as pwg_layers

        else:
            torch = wrapped_import("torch")
            wrapped_import("parallel_wavegan")
            pwg_models = wrapped_import("parallel_wavegan.models")
            pwg_layers = wrapped_import("parallel_wavegan.layers")

        # Initialize PWG
        pwg_config = yaml.load(open(args.pwg_config), Loader=yaml.Loader)
        pyt_device = torch.device("cpu")
        generator = pwg_models.MelGANGenerator(**pwg_config['generator_params'])
        generator.load_state_dict(
            torch.load(args.pwg_checkpoint, map_location="cpu")["model"]["generator"])
        generator.remove_weight_norm()
        pwg_model = generator.eval().to(pyt_device)
        assert pwg_config["generator_params"].get("aux_context_window", 0) == 0  # not implemented otherwise
        pwg_pqmf = pwg_layers.PQMF(pwg_config["generator_params"]["out_channels"]).to(pyt_device)

        with torch.no_grad():
            return pwg_pqmf.synthesis(pwg_model(inputs))
  """

  def __init__(self,
               model_func: ModelFuncType, *, inputs: numpy.ndarray,
               use_non_wrapped_reference: bool = True,
               verify_with_torch: bool = True,
               verify_individual_model_io: bool = True,
               import_torch_params: bool = True,
               export_tf_checkpoint_save_path: Optional[str] = None,
               verify_returnn_standalone_model: bool = True,
               ):
    """
    :param model_func:
      Gets an argument wrapped_import(str) -> module, or None. If None, should import as is.
      It also gets the inputs, converted to the right PyTorch `Tensor` object (either original or wrapped).
    :param inputs:
    """
    self._model_func = model_func
    self._inputs_np = inputs
    self._in_n_batch, self._in_n_feature, self._in_n_time = inputs.shape  # currently assumed...
    self._returnn_in_data_dict = dict(shape=(self._in_n_feature, None), feature_dim_axis=1, time_dim_axis=2)
    self.use_non_wrapped_reference = use_non_wrapped_reference
    self.verify_with_torch = verify_with_torch
    self.verify_individual_model_io = verify_individual_model_io
    self.import_torch_params = import_torch_params
    self.export_tf_checkpoint_save_path = export_tf_checkpoint_save_path
    self._tf_checkpoint_save_path = None  # type: Optional[str]
    self.verify_returnn_standalone_model = verify_returnn_standalone_model
    self._out_ref_np = None  # type: Optional[numpy.ndarray]
    self._torch_namespace = None  # type: Optional[Naming]
    self._out_returnn_np = None  # type: Optional[numpy.ndarray]
    self._returnn_net_dict = None  # type: Optional[Dict[str, Dict[str, Any]]]

  def run(self):
    if self.use_non_wrapped_reference:
      self._run_reference()
    if self.verify_with_torch or self.verify_individual_model_io or self.import_torch_params:
      self._run_traced_orig_torch()
    self._run_torch_returnn_drop_in()
    if self.verify_returnn_standalone_model:
      self._run_returnn_standalone()

  def _run_reference(self):
    """
    The reference, using the original import.

    This is only needed if you want to have this as the reference output,
    for further verification checks.
    Otherwise it can be skipped.
    We can use the outputs from the traced Torch variant
    (:func:`_run_traced_orig_torch`) instead.
    """
    print(">>> Running with standard reference imports...")
    torch.manual_seed(42)
    with torch.no_grad():
      out_ref = self._model_func(None, torch.from_numpy(self._inputs_np))
      assert isinstance(out_ref, torch.Tensor)
      out_ref_np = out_ref.cpu().numpy()
    self._out_ref_np = out_ref_np
    print()

  def _run_traced_orig_torch(self):
    """
    Now with wrapped import. That will also use the original PyTorch code, but wrapped with our custom logic.
    This should not change anything, and still would use the PyTorch logic,
    except that the wrapped classes can collect additional information.
    However, we still will check that we got the same output,
    just to check that there is no subtle bug due to the wrapping logic.
    """
    print(">>> Running with wrapped imports, wrapping original PyTorch...")
    torch.manual_seed(42)
    with torch.no_grad():
      with Naming.make_instance(
            wrap_to_returnn_enabled=False,
            keep_orig_module_io_tensors=self.verify_individual_model_io) as naming:
        wrapped_torch = wrapped_import("torch")
        out_wrapped = self._model_func(wrapped_import, wrapped_torch.from_numpy(self._inputs_np))
        assert isinstance(out_wrapped, WrappedTorchTensor)
        out_wrapped_np = out_wrapped.cpu().numpy()
        print(">>>> Module naming hierarchy:")
        naming.root_namespace.dump()
        print(">>>> Root module calls:")
        pprint(dict(naming.get_root_module_calls()))
        torch_mods_with_params = naming.get_modules_with_params_by_abs_name()
        print(">>>> Modules with params:")
        pprint(dict(torch_mods_with_params))
        self._torch_namespace = naming
    if self._out_ref_np is not None:
      assert self._out_ref_np.shape == out_wrapped_np.shape
      numpy.testing.assert_allclose(self._out_ref_np, out_wrapped_np)
      print(">>>> Looks good!")
    else:
      self._out_ref_np = out_wrapped_np  # just use that as further reference
    print()

  def _run_torch_returnn_drop_in(self):
    print(">>> Running with wrapped Torch import, wrapping replacement for PyTorch...")
    torch.manual_seed(42)
    with tf.compat.v1.Session() as session:
      with Naming.make_instance(
            wrap_to_returnn_enabled=True,
            keep_orig_module_io_tensors=True,  # it's only symbolic anyway in TF
            import_params_from_torch_namespace=self._torch_namespace) as naming:
        assert isinstance(naming, Naming)
        in_returnn = torch_returnn.from_numpy(self._inputs_np)
        assert isinstance(in_returnn, torch_returnn.Tensor)
        x = naming.register_input(in_returnn, Data("data", **self._returnn_in_data_dict))
        out_returnn = self._model_func(wrapped_import_demo, in_returnn)
        assert isinstance(out_returnn, torch_returnn.Tensor)
        out_returnn_ = naming.register_output(out_returnn)
        y, returnn_axis_to_torch_axis = out_returnn_.returnn_data, out_returnn_.returnn_axis_to_torch_axis
        print("RETURNN output:", y, "axis map RETURNN->Torch", returnn_axis_to_torch_axis)
        print(">>>> Module naming hierarchy:")
        naming.root_namespace.dump()
        print(">>>> RETURNN net dict:")
        self._returnn_net_dict = naming.root_namespace.dump_as_returnn_net_dict()
        pprint(self._returnn_net_dict)
        print(">>>> Root module calls:")
        pprint(dict(naming.get_root_module_calls()))
        torch_mods_with_params = naming.get_modules_with_params_by_abs_name()
        print(">>>> Modules with params:")
        pprint(dict(torch_mods_with_params))

      feed_dict = {
        x.placeholder: self._inputs_np,
        x.get_sequence_lengths(): [self._in_n_time] * self._in_n_batch  # not so relevant
      }
      y_, y_size = session.run((y.placeholder, y.get_sequence_lengths()), feed_dict=feed_dict)
      assert isinstance(y_, numpy.ndarray)
      self._out_returnn_np = y_
      print("Output shape:", y_.shape)
      print("Output seq lens:", y_size)
      y_torch = y_.transpose(*[returnn_axis_to_torch_axis[i] for i in range(y_.ndim)])
      print("Output shape (converted to Torch):", y_torch.shape)
      if self._out_ref_np is not None:
        numpy.testing.assert_allclose(self._out_ref_np, y_torch, atol=1e-4, rtol=0)
        print(">>>> Looks good!")

      if self.export_tf_checkpoint_save_path or self.verify_returnn_standalone_model:
        returnn_net = naming.root_namespace.returnn_ctx.network
        returnn_net.print_network_info(name="RETURNN network")
        if self.export_tf_checkpoint_save_path:
          self._tf_checkpoint_save_path = self.export_tf_checkpoint_save_path
        else:
          tmp_dir = tempfile.mkdtemp("tmp-returnn-tf-checkpoint")
          self._tf_checkpoint_save_path = tmp_dir + "/model"
        print(f"Saving TF checkpoint to {self._tf_checkpoint_save_path!r}...")
        returnn_net.global_train_step.load(0, session=session)
        returnn_net.save_params_to_file(filename=self._tf_checkpoint_save_path, session=session)
        print()

  def _run_returnn_standalone(self):
    print(">>> Constructing RETURNN model, load TF checkpoint, run...")
    with tf.compat.v1.Session() as session:
      from returnn.config import Config
      from returnn.tf.network import TFNetwork
      config = Config({
        "extern_data": {"data": self._returnn_in_data_dict},
        "debug_print_layer_output_template": True,
      })
      network = TFNetwork(config=config, train_flag=True)
      network.construct_from_dict(self._returnn_net_dict)
      network.load_params_from_file(filename=self._tf_checkpoint_save_path, session=session)

      x = network.extern_data.get_default_input_data()
      y = network.get_default_output_layer().output
      feed_dict = {
        x.placeholder: self._inputs_np,
        x.get_sequence_lengths(): [self._in_n_time] * self._in_n_batch  # not so relevant
      }
      y_, y_size = session.run((y.placeholder, y.get_sequence_lengths()), feed_dict=feed_dict)
      assert isinstance(y_, numpy.ndarray)
      print("Output shape:", y_.shape)
      numpy.testing.assert_allclose(self._out_returnn_np, y_, atol=1e-4, rtol=0)
      print(">>>> Looks good!")
      print()


def verify_torch_and_convert_to_returnn(
      model_func: ModelFuncType, *,
      inputs: numpy.ndarray) -> Converter:
  """
  :param model_func:
    Gets an argument wrapped_import(str) -> module, or None. If None, should import as is.
    It also gets the inputs, converted to the right PyTorch `Tensor` object (either original or wrapped).
  :param inputs:

  model_func will get called multiple times, with different wrapped_import functions.
  wrapped_import would import some user model code.
  wrapped_import expects that the user model code is still unmodified,
  using the original `import torch` statements.

  See the `readme <..>`_ for further details.
  """
  converter = Converter(model_func=model_func, inputs=inputs)
  converter.run()
  return converter