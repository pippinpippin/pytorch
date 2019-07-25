#pragma once

#include <torch/csrc/distributed/rpc/BuiltinOp.h>
#include <torch/csrc/distributed/rpc/BuiltinRet.h>
#include <torch/csrc/distributed/rpc/FutureMessage.h>
#include <torch/csrc/distributed/rpc/Message.h>
#include <torch/csrc/distributed/rpc/RpcAgent.h>
#include <torch/csrc/distributed/rpc/rpc_headers.h>
#include <torch/csrc/jit/pybind_utils.h>
#include <torch/csrc/utils/pybind.h>


namespace torch {
namespace distributed {
namespace rpc {

py::object to_py_obj(const Message& message);

std::shared_ptr<FutureMessage> py_rpc(
    RpcAgent& agent,
    const std::string& dstName,
    const std::string& opName,
    const py::args& args,
    const py::kwargs& kwargs);

}
}
}
