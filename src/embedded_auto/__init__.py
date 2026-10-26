"""Agent 无关的嵌入式自动化工具。"""

__version__ = "0.1.0"

# 历史路径集中在包边界转发，core 自身不导入上层配置或领域接口。
from ._compat import install as _install_compat

_install_compat()
del _install_compat
