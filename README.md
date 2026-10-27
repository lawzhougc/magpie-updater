# Magpie 更新器（fnOS）

该应用提供一个 fnOS 桌面入口，用于检测 Magpie 官方稳定版，并在管理员确认后更新现有 Docker 容器。

## 行为边界

- 后台启动后立即检查一次，此后每 6 小时检查一次 `yetone/magpie-releases` 的官方 latest Release。
- 桌面页面的“重新检查”会立即刷新状态；状态文件保存到应用的 `var/update-status.json`。
- 更新按钮仅在发现新版本时可用，且仅 fnOS 管理员可以调用。
- 后端不接受镜像名、版本号、命令或路径等来自浏览器的参数。
- 升级前，后端重新读取官方 Release，并验证既有容器必须是受支持的 Magpie 部署形态：
  - 容器名 `magpie`，镜像仓库 `ghcr.io/yetone/magpie`；
  - 配置卷 `/vol1/@appdata/magpie/config:/config`；
  - 端口 `192.168.1.3:3425:3425` 与 `192.168.1.3:3430:3430`；
  - 固定启动命令 `web --addr 0.0.0.0:3430 --no-open`；
  - 保留 `MAGPIE_ADDR`、`MAGPIE_PUBLIC_URL`、`MAGPIE_WEB_KEY`、`TZ` 四个必要环境变量。
- 升级流程为：保存 inspect 备份 → 拉取正式镜像 → 停止并保留旧容器 → 以原有受支持参数启动新容器 → 等待 Docker healthy 且 3425/3430 可连接 → 成功后删除旧容器，但保留旧镜像。
- 若创建或健康检查失败，后端会恢复并启动旧容器。

## 权限说明

应用必须以 root 运行，原因是 Docker Socket 仅允许 root 或 docker 组访问，而 fnOS 包用户默认不能控制既有的 `magpie` 容器。root 权限仅供后端调用预设的 Docker 参数；网页不能调用任意 Shell 命令。

## 构建与安装

```bash
fnpack build --directory /vol1/@appdata/deepseek.harness/home/DSH_DATE/magpie-updater
appcenter-cli install-fpk /vol1/@appdata/deepseek.harness/home/DSH_DATE/magpie-updater.fpk
```

安装后从 fnOS 桌面打开“Magpie 更新器”。系统桌面是否支持第三方应用动态角标由 fnOS 决定；该 FPK 已注册桌面入口，应用页会明确显示“1 个可用更新”。

## 验证

```bash
python3 -m py_compile app/server.py
bash -n cmd/main
fnpack build --directory .
```

可用 Unix Socket 模式验证 `GET /api/status` 以及未携带 `X-Trim-Isadmin` 的 `POST /api/upgrade` 返回 403；不要在未确认升级时向升级接口发送管理员身份请求。
