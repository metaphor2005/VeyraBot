# data/ 目录说明

这个目录是**人格与运行时数据的唯一根目录**：她本来是谁、你们聊出来的记忆、演化后的快照，都在这里。

升级代码时，原则上只需要保留整个 `data/`（或至少 `data/persona/`），代码可以整体替换。

```
data/
├── persona/                     # ★ 机器人本来的数据（原初人格，升级时保留）
│   ├── meta.json                # schema 版本 / 人格名 / 头像文件名
│   ├── profile.json             # 身份档案（Prompt 里的【你的身份档案】）
│   ├── kernel.json              # 人格基线参数 + 中文释义
│   ├── seed.json                # 前史：实体 / 关系 / 成长往事 / 人物档案
│   └── avatar.jpg               # 头像
│
├── corpus/                      # ★ 机器人本来的资料：RAG 本地语料（.md / .txt）
│
├── memory.db                    # 运行时：会话、主题、信念、经验、人格状态、好感度、主动消息
├── vectors/                     # 运行时：主题向量索引（每个空间一个 json）
├── persona_snapshots/           # 运行时：人格演化快照（每个空间一个子目录）
├── ui_settings.json             # 运行时：Web UI 的模型连接设置（存在时才有）
└── user_avatar.json             # 运行时：用户自定义头像（存在时才有）
```

## 两类数据的分界

| 目录 | 谁产生的 | 能否删除 | 升级时 |
|---|---|---|---|
| `persona/` | 作者预先写好的"她本来是谁" | 不建议；删了就失去原初人格 | **必须保留** |
| `corpus/` | 作者预先放的本地资料（RAG） | 不建议；删了 RAG 没有本地语料 | **保留** |
| `memory.db` | 用户与她对话产生的长期记忆 | 可以；删了等于清空记忆 | 保留（或按需重置） |
| `vectors/` | 归档时自动生成的主题索引 | 可以；删后会按主题重建 | 保留；也可以由主题重建 |
| `persona_snapshots/` | 日结/大事触发的人格演化快照 | 可以；删了会回到 v0 基线 | 保留（这是她演化过的证据） |
| `ui_settings.json` | Web UI 设置 | 可以；删了回到 `.env` 默认模型 | 保留或重设 |
| `user_avatar.json` | 用户上传的头像 | 可以 | 保留 |

## 怎么升级

1. **备份 `data/`**（最重要的一步）。
2. 替换/更新代码，但**不要覆盖 `data/persona/`**。
3. 如果新版 `meta.json` 的 `schema_version` 变大，按发布说明迁移字段。
4. 核对数据位置：

   ```bash
   python maintenance.py --persona-info
   ```

   会打印人格数据目录、档案条数、参数个数、前史统计、头像是否存在，以及运行时数据路径。

5. 跑离线回归：

   ```bash
   python tests/smoke_test.py
   ```

6. 启动：

   ```bash
   python api/server.py
   ```

## 怎么只重置"记忆"，保留人格

想清空聊天记录和演化，回到"有前史、但还没和你聊过"的状态：

1. 停掉 API 服务。
2. 备份后删除运行时文件：

   ```
   data/memory.db
   data/vectors/
   data/persona_snapshots/
   data/ui_settings.json      (可选：想重设模型时再删)
   data/user_avatar.json      (可选：想恢复默认头像时再删)
   ```

3. 重新注入前史：

   ```bash
   python maintenance.py --seed-persona --space jiangyan
   ```

4. 启动 `python api/server.py`，Web UI 会自动选择 `jiangyan` 空间。

> 不要删 `data/persona/`。它是人格的"源代码"；`persona_snapshots/` 是她聊过之后演化出来的"运行结果"。

## 修改人格数据

- `profile.json`：身份档案，分组字典。改完重启即可影响 Prompt。
- `kernel.json`：`init` 是 14 个人格基线参数（0~1），`description` 是抽取 Prompt 用的中文释义；两边的键必须一一对应。
- `seed.json`：前史。`entities` 是实体，`relations` 是 `[head, rel, tail, importance]`，`stories` 是成长往事，`profile_topic` 是人物档案主题。
- `avatar.jpg`：江妍的头像，Web UI 通过 `/api/avatar/persona` 读取。
- `corpus/`：RAG 本地资料，放入 `.md` / `.txt` 后重启即可参与检索。

改完建议跑一次：

```bash
python tests/smoke_test.py
```

冒烟测试里的「人格原始数据目录」会校验文件齐全、键一致、以及代码确实读到了这些文件。
