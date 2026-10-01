# 可选 Mac 设置辅助工具

这是手动选择的设置辅助，不会被 INSTALL、START、STATUS 或 STOP 自动调用。已有双向 OAuth 与 folder 的用户优先复用，通常只需核查，不需要再次授权或创建。

在已审阅的 Router 包根目录，使用安装 requirements-router-mac.txt 的独立虚拟环境执行：

```sh
python3 docs/gemini_setup/tools/mac_google_setup.py --help
```

## 三个独立命令

**authorize：** 仅在本人明确批准范围后使用 --ack-drive-readonly，真实 Mac Terminal 运行。仅接受正确 endpoint 的 Desktop installed 配置，拒绝任何 web 字段或混合配置。使用显式 PKCE、127.0.0.1 随机端口、300 秒超时；凭据只写新文件，不覆盖已有文件。本人在浏览器完成 consent。

SDK 参数为 authorization_prompt_message，文案不含 URL 占位符；本次操作抑制 SDK 日志，错误仅输出安全短状态。不要自行添加 HTTP debug。浏览器成功页不等于文件已保存，以 Terminal 的 OAUTH_FILE_SAVED 与 refresh_token_present=true 为准。

**check：** 核验私有 authorized-user 文件与本地 scope 记录。无 folder ID 时不联网，也不证明服务器有效授权。有 --folder-id 时可刷新 token 并只读该精确 folder 的 metadata；不写协议对象，不证明反向 raw probe、Docs CAS 或 Router 配对。

**create-folder：** 仅在 --confirm-create 下创建一个已批准的专用文件夹，先持久化 CREATE_INTENT receipt，再发送一次原始 POST；不做重定向、socket 或认证重放。未知结果保留 receipt 并停止；不换 receipt 名称重跑，不自动改共享权限。

输入、输出和 receipt 使用绝对路径；父目录 0700、私有文件 0600，同一用户所有。拒绝符号链接或不安全文件。完整命令见第 01 章。不要打印或上传 client secret、refresh token 或完整授权 URL。

## 离线测试与待验收

从本目录执行 `python3 -B -m unittest -v test_mac_google_setup`，结果见 ../verification/setup-helper-tests.txt。测试使用虚拟数据和 mock 网络，不做真实 OAuth、Google 写入或模型调用。

独立 SDK mock 执行使用已安装的 google-auth-oauthlib 1.5.0；requirements 的 1.2.2 通过官方源码核对，不能标成真实执行。真实 Mac 浏览器授权、token 保存与 Google folder 创建仍需本人现场批准和验收。
