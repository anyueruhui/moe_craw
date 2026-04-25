# Kmoe 安全测试脚本

## 快速开始（Skill 模式）

已封装为 Claude Code Skill，直接用自然语言对话即可：

```
给我下载《烙印战士》
给我下载《烙印战士》第5卷
下载《进击的巨人》第1到10卷，epub格式
```

Skill 会自动搜索、优先中文版、多结果时让你选择、确认后下载到 `~/Downloads`。

## 手动使用

### 环境准备

```bash
# 创建虚拟环境并安装依赖
uv venv .venv
source .venv/bin/activate
uv pip install requests beautifulsoup4
```

## 获取 Cookie

在浏览器中登录 koz.moe，打开开发者工具 (F12) -> Network -> 随便点击一个请求 -> 复制以下三个 cookie 的值：

| Cookie 名 | 说明 |
|-----------|------|
| `VLIBSID` | Session ID |
| `VOLSKEY` | 签名密钥 |
| `VOLSESS` | Session 版本号 |

## 使用方式

### 1. 仅搜索（不下载）

```bash
python kmoe_crawler.py \
  --cookie-vlibsid "YOUR_VLIBSID" \
  --cookie-volskey "YOUR_VOLSKEY" \
  --cookie-volsess "YOUR_VOLSESS" \
  -s "漫画名称"
```

输出搜索结果列表及每个漫画的详情页 URL。

### 2. 搜索 + 下载第一个结果

```bash
python kmoe_crawler.py \
  --cookie-vlibsid "YOUR_VLIBSID" \
  --cookie-volskey "YOUR_VOLSKEY" \
  --cookie-volsess "YOUR_VOLSESS" \
  -s "烙印战士" -d
```

### 3. 搜索 + 下载全部结果

```bash
python kmoe_crawler.py \
  --cookie-vlibsid "YOUR_VLIBSID" \
  --cookie-volskey "YOUR_VOLSKEY" \
  --cookie-volsess "YOUR_VOLSESS" \
  -s "烙印战士" --download-all
```

### 4. 指定漫画 URL 直接下载

```bash
python kmoe_crawler.py \
  --cookie-vlibsid "YOUR_VLIBSID" \
  --cookie-volskey "YOUR_VOLSKEY" \
  --cookie-volsess "YOUR_VOLSESS" \
  --book-url "https://koz.moe/c/50075.htm"
```

### 5. 下载 epub 格式

```bash
python kmoe_crawler.py \
  --cookie-vlibsid "YOUR_VLIBSID" \
  --cookie-volskey "YOUR_VOLSKEY" \
  --cookie-volsess "YOUR_VOLSESS" \
  --book-url "https://koz.moe/c/50075.htm" \
  --type epub
```

### 6. 只下载部分卷

```bash
# 从第 5 卷开始，最多下载 3 卷
python kmoe_crawler.py \
  --cookie-vlibsid "YOUR_VLIBSID" \
  --cookie-volskey "YOUR_VOLSKEY" \
  --cookie-volsess "YOUR_VOLSESS" \
  --book-url "https://koz.moe/c/50075.htm" \
  --start 4 --max 3
```

### 7. 调整请求间隔

```bash
# 0.3 秒间隔（更快，但更容易触发风控）
python kmoe_crawler.py \
  --cookie-vlibsid "YOUR_VLIBSID" \
  --cookie-volskey "YOUR_VOLSKEY" \
  --cookie-volsess "YOUR_VOLSESS" \
  -s "漫画" -d --delay 0.3
```

## 全部参数

| 参数 | 缩写 | 必填 | 说明 |
|------|------|------|------|
| `--cookie-vlibsid` | | 是 | VLIBSID cookie 值 |
| `--cookie-volskey` | | 是 | VOLSKEY cookie 值 |
| `--cookie-volsess` | | 是 | VOLSESS cookie 值 |
| `--search` | `-s` | 否 | 搜索关键词 |
| `--book-url` | | 否 | 直接指定漫画详情页 URL |
| `--download` | `-d` | 否 | 搜索时下载第一个结果 |
| `--download-all` | | 否 | 搜索时下载全部结果 |
| `--type` | | 否 | 文件格式: `mobi`(默认) 或 `epub` |
| `--start` | | 否 | 从第 N 卷开始（从 0 计数，默认 0） |
| `--max` | | 否 | 最多下载 N 卷（0=全部，默认 0） |
| `--delay` | | 否 | 请求间隔秒数（默认 1.0） |
| `--output` | `-o` | 否 | 下载保存目录（默认 `./downloads`） |

`--search` 和 `--book-url` 二选一，不能同时省略。

## 输出结构

```
downloads/
└── 漫画名称/
    ├── 卷 01.mobi
    ├── 卷 02.mobi
    ├── ...
    └── 卷 N.mobi
```

## 安全测试报告

脚本运行完成后会自动输出安全分析报告，包含：

- 总请求数和耗时统计
- 发现的安全漏洞（captcha 绕过、频率限制缺失等）
- CDN 签名 URL 分析
- Session cookie 安全性评估
- 修复建议

## 注意事项

- Cookie 有效期有限，过期后需重新登录获取
- 下载文件会占用账号额度（quota），注意余额
- `--delay` 建议不低于 0.5 秒，过快可能触发风控
- Cookie 泄露后他人可冒用你的账号，注意保密

## 配置文件

在脚本同目录下创建 `config.json` 可预设所有参数，CLI 参数会覆盖配置：

```json
{
    "vlibsid": "YOUR_VLIBSID",
    "volskey": "YOUR_VOLSKEY",
    "volsess": "YOUR_VOLSESS",
    "type": "mobi",
    "delay": 1.0,
    "output": "./downloads"
}
```

配置后可省略所有 cookie 参数：`python kmoe_crawler.py -s "漫画名" -d`
