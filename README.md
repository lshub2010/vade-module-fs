# fs module

在**白名单根目录**内提供 `list` / `stat` / `read` / `write` / `delete`。
模块的价值不在五个工具，而在那条边界：`../../etc/passwd`、绝对路径越界、
指向外部的软链，全部必须在 `Path.resolve()` 之后被拒。

## 工具

| 工具 | 逻辑面 | 签名 | 结果 | risk | idempotent |
|---|---|---|---|---|---|
| `fs_list` | `fs.list` | `fs_list(path: str)` | `{entries:[{name,kind,size,mtime}], truncated}` | low | true |
| `fs_stat` | `fs.stat` | `fs_stat(path: str)` | `{kind,size,mtime,mode}` | low | true |
| `fs_read` | `fs.read` | `fs_read(path: str, max_bytes: int = 65536)` | `{text,bytes,truncated}` | low | true |
| `fs_write` | `fs.write` | `fs_write(path: str, content: str, create_parents: bool = false)` | `{bytes,created}` | **high** | **false** |
| `fs_delete` | `fs.delete` | `fs_delete(path: str, recursive: bool = false)` | `{deleted}` | **high** | true |

**名字为什么是下划线**：SDK 的 `ToolSpec` 只接受 `^[a-z][a-z0-9_]{0,63}$`，
`.` 不合法，所以 `fs.list` 在 manifest 里的真名是 `fs_list`；module_id 是
`com.example.fs`，core 对模型暴露的名字是 `fs__fs_list`。文档与讨论里仍可用
`fs.list` 指代，代码与 manifest 一律用 `fs_list`。

结果字段的约定：

- `entry.size` / `stat.size`：字节；软链按 lstat 计（长度 = 目标字符串长度）。
- `mtime`：epoch 秒（int），不返回纳秒。
- `stat.mode`：权限位（`st_mode & 0o777`，int，例如 `0o640` = 416）。
- `entry.kind`：`file` | `dir` | `symlink`，**只描述目录项本身**（`scandir` 不跟随）。
- `read.bytes`：返回的 `text` 的 UTF-8 字节数（截断后可能小于文件大小）。
- `write.created`：写之前文件是否不存在；`write.bytes` 是写入内容的字节数。

## 安全模型（本模块的重点）

### 1. 白名单根

```bash
FS_ROOTS=/data/workspace:/tmp/agent     # ':' 分隔，按声明顺序匹配
FS_DATA_DIR=/data/workspace             # 未设置 FS_ROOTS 时的默认根
```

- **每次调用**重新读取环境变量并 `resolve()` 根目录（根自己可以是软链）。
- 两个都没设 → **所有调用** `PERMISSION_DENIED`（fail closed，不是 fail open）。
- 相对路径相对**第一个**根解释；空串 / `"."` 就是第一个根。
- 多个根时按声明顺序取第一个包含该路径的根。

> **不要把 `FS_ROOTS` 配成 `/` 或 `$HOME`。** 那等于把整台机器交给模型，
> 本模块的所有检查都还在，只是没有意义了。容器里只给 `/data/workspace`。

### 2. 一次 resolve，然后才判断

每个工具的第一步都是同一个 `resolve_path()`：

```python
real = (roots[0] / Path(path) if not absolute else Path(path)).resolve(strict=False)
for root in roots:
    if real == root or real.is_relative_to(root):
        return real, root, relative
raise ToolFailure("PERMISSION_DENIED", "path escapes the configured roots")
```

三个必须写对的细节，本模块都写了：

1. **先 resolve 再判断**。`..` 与软链在判断之前就已经被解析掉，不存在
   "判断的是字面量、用的是解析后路径" 的缝隙。
2. **根也 resolve**。根是软链时，比较的是它指向的真实目录。
3. **用 `Path.is_relative_to`，不用字符串 `startswith`**。
   `/data/workspace-evil` 不会骗过 `/data/workspace`；`test_sibling_with_the_same_prefix_is_denied`
   钉住这条。

之后所有操作都作用在**解析后的路径**上，因此不存在"检查 A、操作 B"：

- `read` / `stat` / `list` 打开的就是被检查过的那个路径；
- `write` 用 `O_NOFOLLOW` 打开最后一段（检查后才发现是软链 → `ELOOP` → `PERMISSION_DENIED`）；
- `delete` 用解析后的路径 `unlink`/`rmdir`/`rmtree`，`rmtree` 不跟随树内软链。

### 3. 软链

| 情况 | 行为 |
|---|---|
| 根内软链 → 根外 | 解析后越界 → `PERMISSION_DENIED`（read/stat/write/delete/list 一致） |
| 根内软链 → 根内 | 允许，操作作用在**目标**上（写就是写目标，删就是删目标） |
| 根内软链目录 → 根外 | `list` 该目录 → `PERMISSION_DENIED`；列出**父目录**时它只是一个 `kind=symlink` 的条目，不会被跟随 |
| 根自己是软链 | 允许（根先 resolve） |

`list` 用 `scandir(follow_symlinks=False)` + `lstat` 语义，所以"看一眼"不会把
根外的 inode 暴露出来（连 `size` 都是链接自身的长度）。

### 4. 上限

| 上限 | 值 | 触发 |
|---|---|---|
| 单次读 | 1 MiB（`MAX_IO_BYTES`） | `max_bytes` 超出 `[1, 1048576]` → `INVALID_ARGUMENT` |
| 单次写 | 1 MiB | 内容超出 → `PAYLOAD_TOO_LARGE`，文件不会被创建/截断 |
| `list` 条目 | 10 000（`MAX_LIST_ENTRIES`） | 超出 → 只返回前 10 000 条，`truncated=true` |

`read` 永远只读 `max_bytes + 1` 个字节（多读 1 字节只为判断是否截断），
所以 4 GiB 的文件不会被读进内存。截断**不是错误**：`truncated=true`。

### 5. 错误码与信息

| 码 | 何时 |
|---|---|
| `INVALID_ARGUMENT` | 参数类型不对、路径含 NUL、`max_bytes` 越界、目标是目录/非常规文件、二进制（含 NUL）、非法 UTF-8、非空目录但 `recursive=false` |
| `PERMISSION_DENIED` | 越界（`..` / 绝对路径 / 软链）、未配置根、写/删根目录本身、OS 权限拒绝、`O_NOFOLLOW` 命中 |
| `NOT_FOUND` | 路径不存在、父目录不存在且 `create_parents=false` |
| `PAYLOAD_TOO_LARGE` | 写入内容 > 1 MiB |
| `INTERNAL` | 未归类的 OS 错误（只带 `strerror`，不带路径） |

**错误信息里没有绝对路径**：越界错误连调用方给的路径都不回显，其余错误只带
*相对根* 的路径（根本身显示为 `"."`）。`test_error_messages_never_leak_absolute_paths`
对 `tmp_path` / 根 / 根外目录做子串断言。

### 6. 边界选择

- 写/删**根目录本身**：`PERMISSION_DENIED`（`fs_delete(".", recursive=true)` 不是清空根）。
- `fs_delete` 删不存在的路径：`NOT_FOUND`，不是 `{deleted:false}`。`idempotent=true`
  的含义是"重复调用不会产生额外副作用"，不是"第二次调用返回成功"——把打错的路径
  静默当成删成功更危险。
- `fs_write` 覆盖已有文件不报错（`created=false`）；没有 `if_exists` 开关，
  因为任务面要求最小。
- `fs_read` 的窗口切在多字节 UTF-8 字符中间时，最多回退 3 个字节而不是报"二进制"
  （`test_read_trims_a_multibyte_character_cut_by_the_window`）。
- `stat` 报告**解析后**的对象：`stat("link")` 看到的是目标文件，而 `list` 看到的是
  `kind=symlink` 的目录项。两者回答的是不同问题，README 明确写出来而不是让它成为惊喜。

## 布局

```
modules/fs/
├── manifest.json            generated — never hand-edited
├── pyproject.toml
├── src/fs_module/
│   ├── tools.py             pure functions, stdlib only, no SDK import
│   └── server.py            ModuleApp + ToolSpec, only forwards
├── tests/test_tools.py      runs without vade-sdk installed
├── tests/test_server.py     skipped when vade-sdk is missing
├── Dockerfile               build from the REPO ROOT (needs sdk/python)
└── data/                    module key lives here (gitignored, volume it)
```

`tools.py` 不 import `vade_sdk`（AST 测试钉住），所以工具可以在没有 SDK 的机器上
写完、审完、测完；`server.py` 不碰任何路径，只做 `ToolFailure → ToolError` 的翻译。
`ToolSpec` 由 SDK 从函数签名生成 JSON Schema —— 签名就是 schema，默认值直接进 manifest。

## 运行

```bash
pip install -e sdk/python          # 或 pip install "vade-sdk==0.1.*"
pip install -e modules/fs

pytest modules/fs/tests -q         # 65 passed（无 core；无 SDK 时 server 测试 skip）
python -m fs_module --check        # exit 0，并把 manifest 打到 stdout

FS_ROOTS=/data/workspace \
  python -m fs_module --standalone fs_list --args '{"path":"."}'
# {"ok": true, "result": {"entries": [...], "truncated": false}}

FS_ROOTS=/data/workspace \
  python -m fs_module --standalone fs_read --args '{"path":"../../etc/passwd"}'
# {"ok": false, "error": {"code": "PERMISSION_DENIED", ...}}   exit 1
```

`manifest.json` 由同一条生成路径产出（`--print-manifest` / `--check` / `module/register`
共用一个函数）：

```bash
PYTHONPATH=src python -m fs_module --print-manifest > manifest.json
PYTHONPATH=src python -m fs_module --print-manifest | diff - manifest.json   # 空
```

连 core（需要 core 白名单里有这个模块的 key，且 core 的 Noise 公钥已知）：

```bash
CORE_PUBLIC_KEY=<64 hex> MODULE_KEY_PATH=data/module.key \
  FS_ROOTS=/data/workspace \
  python -m fs_module --core wss://127.0.0.1:8443
```

`concurrency="exclusive"`：一个实例独占根目录。两个实例会互写同一批文件，而
`fs_write` 不是幂等的。

## Frame ceiling（部署前必读）

v1 传输**没有分片**，一帧明文上限 65519 字节（`FRAME_PLAINTEXT_BYTES`）。本模块
本地允许 `fs_read` 返回最多 1 MiB，但超过一帧的结果**发不出去**：transport 会在
`payload > 65519` 时直接 `PAYLOAD_TOO_LARGE` 并断开（fatal）。

所以对 core 部署时把 `max_bytes` 控制在 ~60000 以内（默认 65536 已经贴着上限，
JSON 包装后可能超出）；1 MiB 是本地硬上限（防内存放大），不是"一定能送达"的承诺。
`--standalone` 不经过 transport，不受此限。

## 容器

```bash
docker build -f modules/fs/Dockerfile -t vade/fs:dev .
docker run --rm -v /tmp/agent:/data/workspace vade/fs:dev \
  --standalone fs_list --args '{"path":"."}'
```

镜像里 `FS_DATA_DIR=FS_ROOTS=/data/workspace`，uid 10001，`/data` 之外没有可写层；
`MODULE_KEY_PATH=/data/module.key` 必须挂卷持久化，否则指纹变化会被 core 拒绝
（AMEND-01）。

## 两条不变量

1. `tools.py` 零 SDK import（AST 测试），`server.py` 零业务逻辑。
2. 每个工具的第一行都是 `resolve_path()`；没有任何一条路径能绕过它。
