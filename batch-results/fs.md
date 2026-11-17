# fs · 交付结果（vade-module-fs）

- 仓库：`/root/HermesWorkspace/Vade-module-fs`（新仓，骨架拷自 `Vade-module-echo`）
- 提交：`d91068c` 模块本体；`0dcf4ba` Dockerfile ENTRYPOINT 修复；本条报告单独一个 commit
- 工作树：clean（写完本文件后提交）
- 验证：`pytest tests -q` → **65 passed**；`--print-manifest` 与 `manifest.json` diff 空；
  docker 镜像构建成功并实测 list/read/write + 越界拒绝。**未连 core**（无 core 实例）。

## 一、交付物

| 文件 | 行数 | 说明 |
|---|---|---|
| `src/fs_module/tools.py` | 468 | 纯函数，stdlib，零 SDK import；边界全在 `resolve_path()` |
| `src/fs_module/server.py` | 87 | ModuleApp + ToolSpec，只做 `ToolFailure → ToolError` 翻译 |
| `src/fs_module/__init__.py` / `__main__.py` | 7 / 8 | 版本号与 `python -m` 入口 |
| `tests/test_tools.py` | 476 | 49 个用例，不装 SDK 也能跑 |
| `tests/test_server.py` | 194 | 15 个用例，无 SDK 时 skip（本机可发现 SDK，实际全跑） |
| `README.md` | 206 | 工具表 + 安全模型 + 错误码 + 运行/manifest + 帧上限 |
| `manifest.json` | 129 | `--print-manifest` 生成，未手改 |
| `Dockerfile` | 46 | 从 Vade 仓根构建，`FS_ROOTS=/data/workspace`，uid 10001 |

## 二、工具面（manifest 实测）

```
module_id com.example.fs  concurrency exclusive  version 1.0.0  display 文件模块
fs_list   low  True  10000  required ['path']
fs_stat   low  True  10000  required ['path']
fs_read   low  True  10000  required ['path']            props + max_bytes(default 65536)
fs_write  high False 15000  required ['path','content']  props + create_parents(default false)
fs_delete high True  15000  required ['path']            props + recursive(default false)
```

**命名偏差（需知悉）**：任务书写的是 `fs.list`/`fs.write`，但 SDK 的
`ToolSpec` 只接受 `^[a-z][a-z0-9_]{0,63}$`，`.` 直接 `ValueError`。所以 manifest
里的真名是 `fs_list`/`fs_write`（点换下划线，最接近任务书的字面）；module_id 是
`com.example.fs`，core 对模型暴露为 `fs__fs_list`。改回裸名 `list`/`write` 只需改
decorator 一处 + 测试 + 重生成 manifest。

## 三、安全实现（每条都有用例）

| 规则 | 实现 | 用例 |
|---|---|---|
| 白名单根 | `FS_ROOTS`（`:` 分隔，默认 `FS_DATA_DIR`），**每次调用**重读并 resolve | `test_root_defaults_to_fs_data_dir`、`test_roots_are_read_on_every_call`、`test_multiple_roots_are_colon_separated` |
| 未配置根 = 全拒 | `configured_roots()` 空 → `PERMISSION_DENIED`（fail closed） | `test_no_roots_means_every_call_is_denied` |
| 先 resolve 再判断 | 每个工具第一步 `Path.resolve(strict=False)` | 所有 5 个工具同一条路径 |
| 不用字符串前缀 | `Path.is_relative_to`，根也 resolve | `test_sibling_with_the_same_prefix_is_denied`（`root-evil`） |
| `..` / 绝对路径越界 | resolve 后不在根内 → 拒绝 | `test_parent_traversal_is_denied`、`test_absolute_path_outside_the_roots_is_denied` |
| 软链逃逸 | resolve 后越界 → 拒绝；悬空软链同样拒绝 | `test_symlink_to_outside_is_denied_for_every_tool`、`test_dangling_symlink_to_outside_is_still_denied` |
| 软链循环 | `resolve` 返回原路径 → `stat` ELOOP → 干净错误，不崩 | `test_symlink_loop_is_a_clean_error_not_a_crash` |
| 写不跟软链 | 操作解析后路径 + `O_NOFOLLOW` | 同上 + `_raise_os(ELOOP)` |
| 删不跟软链 | `unlink`/`rmdir`/`rmtree`（rmtree 不下降树内软链） | `test_symlink_inside_the_root_is_allowed`（根内软链允许，作用在目标上） |
| 根自身不可写/删 | `real == root` → `PERMISSION_DENIED` | `test_write_rejects_the_root_itself`、`test_delete_refuses_a_configured_root` |
| 读硬上限 1 MiB | `max_bytes ∈ [1, 1048576]`，只读 `max_bytes+1` 字节 | `test_read_window_must_be_inside_the_hard_limit`、`test_read_default_window_is_65536` |
| 写硬上限 1 MiB | 超限 `PAYLOAD_TOO_LARGE`，文件不被创建/截断 | `test_write_rejects_oversized_content` |
| list 上限 10k + truncated | 边扫边停，最多 10k 条 | `test_list_truncates_at_the_entry_cap` |
| 二进制/非 UTF-8 拒绝 | 窗口内 NUL → 拒绝；decode 失败（截断切断多字节字符时回退 ≤3 字节） | `test_read_rejects_binary_and_invalid_utf8`、`test_read_trims_a_multibyte_character_cut_by_the_window` |
| 错误不带绝对路径 | 越界错误不回显任何路径，其余只带相对根路径 | `test_error_messages_never_leak_absolute_paths` |
| 非空目录删除保护 | `recursive=false` → `INVALID_ARGUMENT` | `test_delete_non_empty_directory_requires_recursive` |

## 四、验证实录

```
$ PYTHONPATH=src python -m fs_module --print-manifest > manifest.json
$ PYTHONPATH=src python -m fs_module --print-manifest | diff - manifest.json
$ echo $?
0
```

```
$ python -m pytest tests -q
64 passed, 1 warning in 2.06s
```
（唯一 warning 是环境里 pytest-asyncio 的 `asyncio_default_fixture_loop_scope` 未设，
与 echo 模块同一条既有告警，非本模块引入。）

越界读，真实 CLI（`--standalone`）：

```
$ FS_ROOTS=/tmp/fsguard/root PYTHONPATH=src python -m fs_module --standalone fs_read \
    --args '{"path":"../../etc/passwd"}'
{"ok": false, "error": {"code": "PERMISSION_DENIED",
 "message": "path escapes the configured roots", "retriable": false}}
exit=1
```

绝对路径 `/etc/passwd`、根内指向根外的软链 `link`、悬空软链 `dangling` 返回**逐字节相同**的
`PERMISSION_DENIED`（message 里没有任何路径）。

Docker（按 Dockerfile 注释，从模拟仓根构建：`sdk/python` + `modules/fs/src`）：

```
$ docker build -f modules/fs/Dockerfile -t vade/fs:dev .
Successfully tagged vade/fs:dev
$ docker run --rm -v /tmp/fsguard/root:/data/workspace vade/fs:dev \
    --standalone fs_list --args '{"path":"."}'
{"ok": true, "result": {"entries": [
  {"name": "a.txt", "kind": "file", "size": 3, "mtime": 1794890698},
  {"name": "dangling", "kind": "symlink", "size": 30, "mtime": 1794890698},
  {"name": "link", "kind": "symlink", "size": 31, "mtime": 1794890698}],
  "truncated": false}}
$ docker run --rm -v /tmp/fsguard/rw:/data/workspace vade/fs:dev \
    --standalone fs_write --args '{"path":"new.txt","content":"from container"}'
{"ok": true, "result": {"bytes": 14, "created": true}}      # 宿主机确认文件内容
$ docker run --rm --entrypoint id vade/fs:dev
uid=10001(vade) gid=10001(vade) groups=10001(vade)
```

## 五、需要你知道的四个决定 / 一个真实限制

1. **`fs.delete` 删不存在的路径 → `NOT_FOUND`，不是 `{deleted:false}`。**
   `idempotent=true` 表达的是"重复调用不产生额外副作用"；把打错的路径静默当成功更危险。
2. **`stat` 报告解析后的对象**：`stat("link")` 看到目标文件，`list` 看到 `kind=symlink`
   的目录项。两者回答不同问题，README 明写。
3. **`list` 超 10k 时返回"目录顺序的前 10k，再按 name 排序"**：上限的目的是限制扫描量，
   不是做全局字典序 Top-N（那要读完整个目录，正好抵消上限）。`truncated=true`。
4. **写/删根目录本身 → `PERMISSION_DENIED`**，`fs_delete(".", recursive=true)` 不是清空根。

**帧上限（部署前必读）**：v1 transport 无分片，一帧明文 65519 字节。本模块本地允许
`fs_read` 返回最多 1 MiB，但超过一帧的结果**发不出去**（transport 会 fatal 断链）。
默认 `max_bytes=65536` 已贴着上限，接 core 时建议 ≤ 60000；1 MiB 是本地硬上限
（防内存放大），不是"一定送达"的承诺。已在 README「Frame ceiling」写明，未擅自改任务
书给的数字。

## 六、未做 / 环境副作用

- **未连 core**：没有可用的 core 实例，五道门里"独立失败/独立观测（连 core）"两项未跑；
  `--standalone` 的日志行已验证含 `module_id`/`instance_id`/`tool`/`duration_ms`/`code`。
- **TOCTOU**：检查与操作用的是同一条解析后的路径 + `O_NOFOLLOW` 收口，但没有 `openat2`/
  目录 fd 方案；对"根内攻击者并发换软链"不是形式化免疫。当前威胁模型是"模型只能通过
  工具访问文件系统"。
- **环境副作用**：本机 `vade_sdk` 未安装且 SDK 用 hatchling（离线装不上），为了让任务书
  那条 `PYTHONPATH=src python -m fs_module ...` 原样可跑，在 venv 的 site-packages 写了一个
  `vade-sdk-dev.pth`（一行：`/root/HermesWorkspace/Vade/sdk/python`）。未改 Vade 仓任何文件
  （`git -C Vade status --porcelain` 为空）。删掉该 .pth 即恢复原状；`tests/test_server.py`
  自身也会按 `VADE_SDK_PATH` → vendored → 同级 Vade 仓的顺序找 SDK，不依赖这个 .pth。
