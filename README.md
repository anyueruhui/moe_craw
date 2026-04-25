# Kmoe Crawler

从 [koz.moe](https://koz.moe) 批量下载漫画（epub/mobi 格式），支持多账号自动轮换。

## 功能

- 搜索漫画并批量下载
- 优先 epub 格式，无 epub 时自动回退 mobi
- 多账号轮换：某账号额度耗尽或 403 时自动切换下一个
- 自动登录：只需配置邮箱密码，脚本自动获取 session
- 运行时状态与配置分离（`config.json` / `state.json`）
- 文件名格式：`漫画名_卷名.ext`

## 快速开始

```bash
# 克隆仓库
git clone https://github.com/YOUR_USERNAME/moe_craw.git
cd moe_craw

# 创建虚拟环境
uv venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
uv pip install -r requirements.txt

# 配置账号
cp config.example.json config.json
# 编辑 config.json，填入你的 koz.moe 账号

# 下载漫画
python kmoe_crawler.py -s "烙印战士" -d
```

## 配置文件

### config.json — 用户设定（只读，不会被脚本修改）

```json
{
    "accounts": [
        {"email": "user1@example.com", "passwd": "password1"},
        {"email": "user2@example.com", "passwd": "password2"}
    ],
    "type": "epub",
    "delay": 1.0,
    "output": "~/Downloads"
}
```

| 字段 | 说明 | 默认值 |
|------|------|--------|
| `accounts` | 账号列表，支持多个 | 必填 |
| `accounts[].email` | koz.moe 登录邮箱 | 必填 |
| `accounts[].passwd` | koz.moe 登录密码 | 必填 |
| `type` | 下载格式：`epub` 或 `mobi` | `epub` |
| `delay` | 请求间隔（秒） | `1.0` |
| `output` | 下载目录 | `~/Downloads` |

### state.json — 运行时状态（自动管理，可随时删除）

脚本自动维护，存储 session cookie、活跃账号索引、账号耗尽标记等。删除后下次运行会自动重新登录。

## 使用方式

### 命令行

```bash
# 搜索
python kmoe_crawler.py -s "一拳超人"

# 搜索并下载第一个结果
python kmoe_crawler.py -s "一拳超人" -d

# 下载指定 URL 的漫画
python kmoe_crawler.py --book-url "https://koz.moe/c/11842.htm" -d

# 仅查看某本漫画的卷列表（不下载）
python kmoe_crawler.py --book-url "https://koz.moe/c/11842.htm"

# 只下载最新一卷（先查看卷列表确认总数，再加 -d 和 --start 下载）
python kmoe_crawler.py --book-url "https://koz.moe/c/11842.htm" --start 15 --max 1 -d

# 下载前 5 卷
python kmoe_crawler.py -s "烙印战士" -d --max 5

# 强制重新登录
python kmoe_crawler.py -s "漫画" -d --login

# mobi 格式
python kmoe_crawler.py -s "漫画" -d --type mobi

# 自定义下载目录
python kmoe_crawler.py -s "漫画" -d -o ~/Documents/manga
```

### 全部参数

| 参数 | 缩写 | 说明 |
|------|------|------|
| `--search` | `-s` | 搜索关键词 |
| `--book-url` | | 漫画详情页 URL |
| `--download` | `-d` | 下载搜索结果的第一个 |
| `--download-all` | | 下载搜索到的全部漫画 |
| `--type` | | 格式：`epub`（默认）或 `mobi` |
| `--start` | | 从第 N 卷开始（0-based） |
| `--max` | | 最多下载 N 卷（0=全部） |
| `--delay` | | 请求间隔秒数（默认 1.0） |
| `--output` | `-o` | 下载保存目录 |
| `--login` | | 强制重新登录 |

`--search` 和 `--book-url` 二选一。

## Claude Code Skill 集成

本项目附带一个 Claude Code Skill，可以用自然语言下载漫画。

### 安装

```bash
# 1. 设置环境变量（加到 ~/.zshrc 或 ~/.bashrc 中）
echo 'export MOE_CRAW_DIR="$(cd "$(dirname "$0")" && pwd)"' >> ~/.zshrc
# 或者手动指定绝对路径：
echo 'export MOE_CRAW_DIR="/path/to/moe_craw"' >> ~/.zshrc
source ~/.zshrc

# 2. 复制 skill 到 Claude Code 配置目录
mkdir -p ~/.claude/skills/kmoe-download
cp SKILL.md ~/.claude/skills/kmoe-download/SKILL.md
```

### 使用

在 Claude Code 中直接说：

```
给我下载《烙印战士》第5卷
下载一拳超人最新的5卷
下载《葬送的芙莉莲》全部
```

Skill 会自动：搜索 → 筛选中文版优先 → 多结果让你选择 → 确认范围 → 执行下载 → 报告结果。

## 多账号轮换

当某个账号遇到以下情况时，自动切换到下一个账号重试：
- 403 错误（session 过期或权限不足）
- 额度耗尽
- 登录失败

所有账号耗尽时停止下载，下次运行时自动重置状态。

## 项目结构

```
moe_craw/
├── kmoe_crawler.py      # 主脚本
├── config.example.json   # 配置示例
├── config.json           # 用户配置（gitignore，不提交）
├── state.json            # 运行时状态（gitignore，自动管理）
├── SKILL.md              # Claude Code Skill 定义
├── requirements.txt      # Python 依赖
└── .gitignore
```

## 注意事项

- 请使用自己注册的账号，遵守站点的使用条款
- 下载文件会消耗账号额度
- `--delay` 建议不低于 0.5 秒
- `config.json` 包含明文密码，不要提交到公开仓库

## License

MIT
