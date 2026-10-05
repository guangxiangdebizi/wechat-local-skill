# 第三方来源与修改说明

`src/wechat_local/vendor/replica_db.py` 来自 [fanyuantaier/wechatauto-replica](https://github.com/fanyuantaier/wechatauto-replica) 的 `wechatauto/db.py`。

- 固定上游提交：`91dc0e1a601013b759b261061d8f7642f736af90`，上游版本 1.2.4.4。
- 上游协议：Apache-2.0，原文见 `licenses/wechatauto-replica.txt`。
- 唯一的 vendored 源码修改是 logger 导入路径。账号选择、隐私保护和 DPAPI 缓存由独立子类实现。该子类还显式修正当前消息分片的发送者映射：使用各分片 `Name2Id`，不使用资源库映射或硬编码的自身 sender ID；vendored 文件未为此改动。
- 默认运行时没有包含或导入上游 GUI、OCR、鼠标/键盘、可访问性热激活或发送驱动。
- 不分发腾讯客户端可执行文件、DLL 或用户数据库。

原生写入接口研究参考了 [aixed/WeChat-Hook](https://github.com/aixed/WeChat-Hook)。该项目文档中的目标版本是 4.1.10.27，其偏移不能当作 4.1.15.13 的有效调用配置。本项目运行时没有合并该仓库的代码。

第三方许可证原文保持原语言，中文说明不替代许可证本身。

独立的控件实验可选依赖 uiautomation 2.0.29（Apache-2.0）和 comtypes 1.4.17；这些库通过包管理器安装，没有将其源代码或二进制复制进本仓库。控件实验的已知版本可访问性标志参考了上述固定的 wechatauto-replica 代码，并增加哈希校验与结束恢复；没有导入其 GUI、OCR、坐标或发送驱动。
