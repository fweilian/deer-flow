# backend/packages/harness/deerflow 本地文件系统依赖分析

## 按模块分类

| 模块 | 文件 | 主要操作 |
|------|------|----------|
| **配置加载** | `app_config.py`, `agents_config.py`, `extensions_config.py` | `open()`, `yaml.safe_load()`, `json.load()`, `Path.exists()` |
| **路径管理** | `paths.py` | `Path` 对象构造、`mkdir`、`resolve`、`rmtree`、`shutil` |
| **内存存储** | `storage.py` | JSON 文件读写 + 原子替换 (`temp_path.replace()`) |
| **上传管理** | `manager.py` | `mkdir`、`scandir`、`is_dir()`、`is_file()`、`unlink()` |
| **技能系统** | `manager.py`, `parser.py`, `validation.py`, `loader.py`, `installer.py` | 存在检查、读写文本、`mkdir`、ZIP 解压 |
| **沙箱** | `local_sandbox.py`, `tools.py`, `search.py`, `list_dir.py` | 文件读写、glob、grep、路径解析 |
| **运行时存储** | `_sqlite_utils.py` | SQLite 数据库父目录创建 |
| **凭证加载** | `credential_loader.py` | `expanduser()`、`exists()`、`is_dir()`、`read_text()` |
| **内置工具** | `view_image_tool.py`, `present_file_tool.py`, `setup_agent_tool.py` | 存在检查、读写文件、`write_text()` |
| **中间件** | `uploads_middleware.py` | `is_file()`、`open()`、路径操作 |
| **文件转换** | `file_conversion.py` | `pymupdf.open()`、PDF 读写 |
| **MCP 缓存** | `cache.py` | `exists()`、`getmtime()` |

## 详细依赖列表

### 1. 配置加载 (YAML/JSON)

**`config/app_config.py`**
- L43-45: `Path(__file__).resolve().parents[4]` — 解析 backend 目录
- L82-83: `Path(config_path)` + `Path.exists(path)` — 解析并检查配置路径
- L87-88: `Path(os.getenv("DEER_FLOW_CONFIG_PATH"))` — 环境变量配置路径
- L93: `path.exists()` — 搜索配置文件
- L110: `open(resolved_path)` + `yaml.safe_load()` — 读取解析 YAML
- L185: `candidate.exists()` — 查找 config.example.yaml
- L196-197: `open(example_path)` + `yaml.safe_load()` — 读取示例配置做版本检查

**`config/extensions_config.py`**
- L93-94, 98-99, 103, 111: `path.exists()` — 解析扩展配置
- L135: `open(resolved_path)` + `json.load()` — 读取 JSON 配置

**`config/agents_config.py`**
- L64: `agent_dir.exists()` — 检查 agent 目录
- L67: `config_file.exists()` — 检查 agent config.yaml
- L71: `open(config_file)` + `yaml.safe_load()` — 读取 agent 配置
- L101, 103: `soul_path.exists()` + `soul_path.read_text()` — 读取 SOUL.md
- L115, 121: `agents_dir.exists()` + `entry.is_dir()` — 扫描 agents 目录

### 2. 路径管理

**`config/paths.py`**
- L14: `Path(__file__).resolve().parents[4]` — 从模块位置推导 base 目录
- L37-41: `PureWindowsPath` / `Path` — 路径风格统一
- L80: `Path(base_dir).resolve()` — 解析 base 目录
- L94, 100: `Path(env)` — 解析 DEER_FLOW_HOST_BASE_DIR
- L110: `Path(env_home).resolve()` — 解析 DEER_FLOW_HOME
- L115-191: 所有属性方法返回 `Path` 对象:
  - `memory_file` → `base_dir / "memory.json"`
  - `user_md_file` → `base_dir / "USER.md"`
  - `agents_dir` → `base_dir / "agents"`
  - `agent_dir(name)` → `agents_dir / name.lower()`
  - `thread_dir(thread_id)` → `base_dir / "threads" / thread_id`
  - `sandbox_work_dir`, `sandbox_uploads_dir`, `sandbox_outputs_dir` → 线程子目录
  - `acp_workspace_dir` → 线程级 ACP 工作区
- L236: `d.mkdir(parents=True, exist_ok=True)` — 创建线程目录
- L245: `thread_dir.exists()` — 检查线程目录
- L246: `shutil.rmtree(thread_dir)` — 删除线程目录
- L273-274: `base.resolve()` + `(base / relative).resolve()` — 虚拟路径映射解析

### 3. 内存存储

**`agents/memory/storage.py`**
- L92: `Path(config.storage_path)` — 从配置获取存储路径
- L100: `file_path.exists()` — 检查内存文件是否存在
- L104: `open(file_path)` + `json.load()` — 读取内存 JSON
- L116, 138: `file_path.stat().st_mtime` — 获取修改时间用于缓存失效
- L151: `file_path.parent.mkdir(parents=True, exist_ok=True)` — 创建父目录
- L158-159: `open(temp_path, "w")` + `json.dump()` — 写入临时文件
- L161: `temp_path.replace(file_path)` — 原子替换

### 4. 上传管理

**`uploads/manager.py`**
- L42: `base.mkdir(parents=True, exist_ok=True)` — 创建上传目录
- L62: `Path(filename).name` — 提取文件名用于清理
- L89: `Path(name).stem, Path(name).suffix` — 文件名唯一性处理
- L123: `directory.is_dir()` — 检查目录是否存在
- L129: `entry.is_file(follow_symlinks=False)` — 过滤目录列表中的文件
- L137: `Path(entry.name).suffix` — 获取文件扩展名
- L166: `file_path.is_file()` — 检查文件是否存在
- L169: `file_path.unlink()` — 删除文件

### 5. 技能系统

**`skills/manager.py`**
- L33: `path.mkdir(parents=True, exist_ok=True)` — 创建自定义技能目录
- L56: `path.mkdir(parents=True, exist_ok=True)` — 创建历史目录
- L64, 69, 73: `get_public_skill_dir(name) / SKILL_FILE_NAME` + `.exists()` — 检查技能存在
- L88-100: `Path(relative_path)` — 验证支持文件路径
- L109-111: `temp_skill_dir.mkdir()` + `write_text()` — 创建临时技能目录验证
- L120: `path.parent.mkdir(parents=True, exist_ok=True)` — 创建父目录
- L121: `tempfile.NamedTemporaryFile` — 创建临时文件用于原子写入
- L129: `history_path.parent.mkdir(parents=True, exist_ok=True)` — 创建历史目录
- L134: `history_path.open("a")` + `f.write()` — 追加历史 JSONL
- L141-147: `history_path.exists()` + `read_text()` + `json.loads()` — 读取历史
- L157-159: `skill_file.exists()` + `read_text()` — 读取自定义技能内容

**`skills/parser.py`**
- L54: `skill_file.exists()` — 检查 SKILL.md 是否存在
- L55: `skill_file.name` — 获取文件名用于验证
- L56: `skill_file.read_text()` — 读取技能 markdown
- L73: `Path(skill_file.parent.name)` — 从父目录推导相对路径

**`skills/validation.py`**
- L25: `skill_md.exists()` — 检查技能文件是否存在
- L28: `skill_md.read_text()` — 读取技能内容用于验证
- L41: `yaml.safe_load()` — 解析 YAML frontmatter

**`skills/loader.py`**
- L19: `Path(__file__).resolve().parent.parent.parent.parent.parent` — 解析到 backend 目录
- L55: `skills_path.exists()` — 检查 skills 目录是否存在
- L63: `category_path.exists()` + `category_path.is_dir()` — 检查分类目录
- L72: `Path(current_root) / "SKILL.md"` — 构建 SKILL.md 路径

**`skills/installer.py`**
- L32, 34-36: `PurePosixPath` / `PureWindowsPath` — 技能归档路径标准化
- L68: `items[0].is_dir()` — 检查归档是否包含目录
- L100: `dest_root.joinpath(*PurePosixPath(...).parts)` — 构建目标路径
- L103-106: `member_path.parent.mkdir()` + `is_dir()` + `mkdir()` — 创建目录结构
- L109, 137-140: `zip_ref.open()` + `member_path.open("wb")` — 从 zip 解压文件
- L149: `custom_dir.mkdir(parents=True, exist_ok=True)` — 创建自定义技能目录
- L173: `target.exists()` — 复制前检查目标是否存在

### 6. 客户端文件操作

**`client.py`**
- L189: `Path(fd.name).replace(path)` — 从临时文件移动到目标
- L192: `Path(fd.name).unlink(missing_ok=True)` — 清理临时文件
- L1059-1064: `Path(f)` + `.exists()` + `.is_file()` — 验证上传文件
- L1194, 1196: `actual.exists()` + `actual.is_file()` — 验证产物文件
- L1200: `actual.read_bytes()` — 读取产物文件内容

### 7. 沙箱文件操作

**`sandbox/local/local_sandbox.py`**
- L43: `os.path.isabs(shell)` — 检查 shell 路径是否绝对
- L44: `os.path.isfile()` + `os.access(shell, os.X_OK)` — 验证 shell 可执行
- L48: `shutil.which(shell)` — 在 PATH 中查找 shell
- L76, 82: `Path(resolved_path).resolve()` — 路径解析
- L113: `Path(local_path) / relative` — 路径构造
- L130: `Path(normalized_path).resolve()` — 规范化路径
- L134: `Path(mapping.local_path).resolve()` — 解析映射本地路径
- L167: `Path(mapping.local_path).resolve()` — 转义路径用于正则
- L318, 343: `open(resolved_path)` — 读取文件内容
- L336: `os.path.dirname(resolved_path)` — 获取目录路径
- L394: `open(resolved_path, "wb")` — 写入二进制文件

**`sandbox/tools.py`**
- L133, 142-143, 160-165, 205, 218: `Path(m.host_path).exists()` — 检查挂载路径存在
- L184: `Path(workspace_path).parent.parent.name` — 从工作区路径提取 thread_id
- L264-269: `Path(resolved).resolve()` + `.relative_to()` — 解析验证沙箱路径
- L448-493: 多个 `Path(p).resolve()` 调用用于路径解析

**`sandbox/local/local_sandbox_provider.py`**
- L41: `skills_path.exists()` — 检查 skills 目录
- L55: `Path(mount.host_path)` — 从挂载配置构建主机路径
- L82: `host_path.exists()` — 检查主机路径存在

**`sandbox/search.py`**
- L110, 112, 158, 160: `root.exists()` + `root.is_dir()` — 验证搜索根目录
- L119, 172, 174: `Path(current_root).relative_to(root)` — 计算相对路径
- L125, 135, 178, 192, 216: `Path(current_root) / name` — 构建文件路径
- L207: `path.open("rb")` — 打开文件读取

**`sandbox/local/list_dir.py`**
- L20: `Path(path).resolve()` — 解析目录路径
- L22: `root_path.is_dir()` — 检查是否是目录
- L35, 39: `item.is_dir()` — 区分目录和文件

### 8. 运行时存储 (SQLite)

**`runtime/store/_sqlite_utils.py`**
- L28: `pathlib.Path(conn_str).parent.mkdir(parents=True, exist_ok=True)` — 创建 SQLite 数据库父目录

### 9. 凭证加载

**`models/credential_loader.py`**
- L62: `Path(configured_path).expanduser()` — 展开路径中的 ~
- L69: `Path(home).expanduser()` — 获取主目录路径
- L74: `path.exists()` — 检查凭证文件是否存在
- L77: `path.is_dir()` — 检查是否是目录
- L82: `path.read_text()` + `json.loads()` — 读取凭证 JSON
- L119, 133: `Path(override_path).expanduser()` — 展开路径

### 10. MCP 缓存

**`mcp/cache.py`**
- L180: `config_path.exists()` — 检查配置文件是否存在
- L181: `os.path.getmtime(config_path)` — 获取文件修改时间

### 11. 内置工具

**`tools/builtins/view_image_tool.py`**
- L42: `Path(actual_path)` — 构造路径
- L49, 55: `path.exists()` + `path.is_file()` — 验证文件
- L81: `open(actual_path, "rb")` — 读取图片文件

**`tools/builtins/present_file_tool.py`**
- L63, 70: `Path(outputs_path).resolve()` + `Path(filepath).expanduser().resolve()` — 路径解析

**`tools/builtins/setup_agent_tool.py`**
- L36-37, 63: `agent_dir.exists()` + `agent_dir.mkdir()` — 创建 agent 目录
- L46: `open(config_file, "w")` + `yaml.dump()` — 写入 agent 配置
- L50: `soul_file.write_text()` — 写入 SOUL.md
- L65: `shutil.rmtree(agent_dir)` — 错误时清理

### 12. 技能管理工具

**`tools/skill_manage_tool.py`**
- L128-199: 多个 `skill_file.read_text()`, `.exists()`, `.write_text()` 操作
- L171: `shutil.rmtree(skill_dir)` — 删除技能目录

**`tools/builtins/invoke_acp_agent_tool.py`**
- L47: `work_dir.mkdir(parents=True, exist_ok=True)` — 创建 ACP 工作目录
- L132: `shutil.which("codex")` — 检查 codex 是否在 PATH 中

### 13. 上传中间件

**`agents/middlewares/uploads_middleware.py`**
- L36: `md_path.is_file()` — 检查 markdown 文件是否存在
- L47: `md_path.open()` — 读取 markdown 文件
- L94, 97: `Path(filename).name` + `uploads_dir / filename` + `.is_file()` — 验证上传路径
- L172-181: `Path(filename).suffix` + `.is_file()` — 文件扩展名和存在检查
- L232-234: `uploads_dir.exists()` + `.is_file()` — 检查现有上传

### 14. 文件转换

**`utils/file_conversion.py`**
- L63: `pymupdf.open(str(file_path))` — 打开 PDF 文件
- L161: `md_path.write_text()` — 写入 markdown 输出
- L257: `md_path.open()` — 读取转换后的 markdown

### 15. 技能配置

**`config/skills_config.py`**
- L8: `Path(__file__).resolve().parents[5]` — 解析到项目根目录
- L32: `Path(self.path)` — 构造 skills 路径

## 关键发现

1. **所有路径均为绝对路径** — 通过 `Path(__file__).resolve().parents[n]` 计算，不依赖 `cwd`
2. **配置文件** — YAML (`app_config.py`, `agents_config.py`) 和 JSON (`extensions_config.py`)
3. **原子写入** — `storage.py` 使用 `tempfile` + `replace()` 实现安全写入
4. **技能系统** — 大量文件系统操作：读取、验证、安装、卸载 SKILL.md
5. **沙箱工具** — 文件搜索、内容读取、路径规范化
6. **SQLite** — `_sqlite_utils.py` 确保数据库父目录存在
