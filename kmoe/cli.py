"""CLI 入口：参数解析与主流程"""

import argparse
import time
from pathlib import Path

from .auth import AccountManager
from .config import DEFAULT_OUTPUT, load_config
from .crawler import KmoeCrawler


def main() -> None:
    cfg = load_config()
    mgr = AccountManager(cfg)
    mgr.reset_accounts()

    parser = _build_parser(cfg)
    args = parser.parse_args()

    # 获取 cookies：CLI 参数 > state.json > 自动登录
    cookies = _resolve_cookies(args, mgr, parser)
    file_type = 1 if args.type == "mobi" else 2

    with KmoeCrawler(cookies, delay=args.delay, account_manager=mgr, workers=args.workers) as crawler:
        start_time = time.time()
        _dispatch(args, crawler, file_type)
        elapsed = time.time() - start_time
        _security_report(crawler, elapsed)


def _build_parser(cfg: dict) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Kmoe 站点安全测试 - 多账号自动轮换"
    )
    parser.add_argument("--cookie-vlibsid")
    parser.add_argument("--cookie-volskey")
    parser.add_argument("--cookie-volsess")
    parser.add_argument("--search", "-s", help="搜索关键词")
    parser.add_argument("--book-url", help="漫画详情页 URL")
    parser.add_argument("--download", "-d", action="store_true", help="执行下载")
    parser.add_argument("--download-all", action="store_true", help="下载搜索到的所有漫画")
    parser.add_argument("--type", choices=["mobi", "epub"], default=cfg.get("type", "epub"))
    parser.add_argument("--start", type=int, default=cfg.get("start", 0), help="从第 N 卷开始")
    parser.add_argument("--max", type=int, default=cfg.get("max", 0), help="最多下载 N 卷 (0=全部)")
    parser.add_argument("--category", help="按分类过滤 (單行本/話/番外篇)")
    parser.add_argument("--delay", type=float, default=cfg.get("delay", 1.0), help="请求间隔(秒)")
    parser.add_argument("--output", "-o", default=cfg.get("output", DEFAULT_OUTPUT))
    parser.add_argument("--login", action="store_true", help="强制重新登录")
    parser.add_argument("--workers", type=int, default=20, help="分块下载线程数 (1=单线程)")
    return parser


def _resolve_cookies(
    args: argparse.Namespace, mgr: AccountManager, parser: argparse.ArgumentParser
) -> dict:
    vlibsid = args.cookie_vlibsid
    volskey = args.cookie_volskey
    volsess = args.cookie_volsess

    if not all([vlibsid, volskey, volsess]):
        active = mgr.get_active_cookies()
        if active:
            vlibsid, volskey, volsess = active["VLIBSID"], active["VOLSKEY"], active["VOLSESS"]

    if args.login or not all([vlibsid, volskey, volsess]):
        idx = mgr.active_index
        auto_cookies = mgr.login(idx)
        if auto_cookies:
            vlibsid, volskey, volsess = (
                auto_cookies["VLIBSID"],
                auto_cookies["VOLSKEY"],
                auto_cookies["VOLSESS"],
            )
        elif not all([vlibsid, volskey, volsess]):
            parser.error(
                "Cookie 缺失且自动登录失败。请在 config.json 中配置 accounts，"
                "或手动填写 cookie。"
            )

    # 显示当前使用的账号
    if mgr.active_email:
        print(f"[*] 当前账号: {mgr.active_email}")

    return {"VLIBSID": vlibsid, "VOLSKEY": volskey, "VOLSESS": volsess}


def _dispatch(args: argparse.Namespace, crawler: KmoeCrawler, file_type: int) -> None:
    output = Path(args.output).expanduser()

    category = getattr(args, "category", None)

    if args.search:
        results = crawler.search(args.search)
        if args.download_all and results:
            for r in results:
                print(f"\n{'─' * 50}")
                crawler.batch_download_book(
                    r["book_url"],
                    save_dir=output,
                    file_type=file_type,
                    start_vol=args.start,
                    max_vols=args.max,
                    category=category,
                )
        elif args.download and results:
            crawler.batch_download_book(
                results[0]["book_url"],
                save_dir=output,
                file_type=file_type,
                start_vol=args.start,
                max_vols=args.max,
                category=category,
            )
        elif results:
            print(f"\n  添加 -d 下载第一个结果，--download-all 下载全部")
            for r in results:
                print(f"    {r['book_url']}")

    elif args.book_url:
        if args.download:
            crawler.batch_download_book(
                args.book_url,
                save_dir=output,
                file_type=file_type,
                start_vol=args.start,
                max_vols=args.max,
                category=category,
            )
        else:
            _show_book_info(crawler, args.book_url)
    else:
        from .crawler import KmoeCrawler as _  # noqa: only for triggering import

        import sys

        sys.argv = [sys.argv[0], "--help"]
        _build_parser(load_config()).parse_args()


def _show_book_info(crawler: KmoeCrawler, book_url: str) -> None:
    detail = crawler.get_book_detail(book_url)
    if detail and detail["data_hash"]:
        volumes = crawler.get_volumes(detail["data_hash"])
        if volumes:
            cats: dict[str, list[dict]] = {}
            for v in volumes:
                cat = v.get("category", "其他")
                cats.setdefault(cat, []).append(v)

            print(f"\n  添加 -d 下载此漫画，--category 按分类过滤")
            print(f"    共 {len(volumes)} 个条目")
            for cat, cat_vols in cats.items():
                print(f"\n    【{cat}】({len(cat_vols)} 个)")
                for i, v in enumerate(cat_vols):
                    print(f"      {i + 1:02d}. {v.get('name', '?')} ({v.get('pages', '?')}p, {v.get('size_mobi', '?')}MB)")
        else:
            print("[!] 无卷数据")
    else:
        print("[!] 无法获取漫画信息")


def _security_report(crawler: KmoeCrawler, elapsed: float) -> None:
    print("\n" + "=" * 60)
    print("  安全测试报告 - Kmoe 自动化流程检测")
    print("=" * 60)
    print(f"  总请求数: {crawler.request_count}")
    print(f"  总耗时: {elapsed:.1f}s")
    print(f"  平均 RPS: {crawler.request_count / max(elapsed, 0.1):.2f}")
    print()
    print("  [CRITICAL] 发现的安全问题:")
    print()
    print("  1. captcha_show() 无实际 CAPTCHA 验证")
    print("     - 函数名暗示有验证码，但实际只是下载分发逻辑")
    print("     - 直接调用 /getdownurl.php 即可绕过")
    print()
    print("  2. /dl/ 路径有保护(403)，但 /getdownurl.php 完全等价")
    print("     - /getdownurl.php 返回的 CDN URL 可直接下载")
    print("     - 两条路径达到同样效果，/dl/ 的保护形同虚设")
    print()
    print("  3. book_data.php hash 包含用户 ID 明文")
    print("     - hash 格式: <timestamp>X<bookid><uid><hmac>")
    print("     - 用户 ID 可被提取，用于用户枚举")
    print()
    print("  4. Session cookie 滚动更新")
    notes = set(crawler.security_notes)
    if notes:
        for note in notes:
            print(f"     - {note}")
    else:
        print("     - 未检测到 cookie 轮换（可能与请求量有关）")
    print()
    print("  5. CDN 签名 URL 分析:")
    print("     - 封面图签名过期时间 ~2035 年（过长）")
    print("     - 下载文件签名含用户 ID (u=xxx)，可追溯")
    print("     - 但签名 URL 一旦获取，在过期前可无限次下载")
    print()
    print("  [建议]")
    print("  - 为 /getdownurl.php 增加真正的 CAPTCHA 或 TOTP 验证")
    print("  - 增加 API 频率限制 (如: 每分钟最多 N 次下载)")
    print("  - 缩短 CDN 签名 URL 有效期至 1-6 小时")
    print("  - 考虑增加 User-Agent/行为分析来检测自动化")
    print("  - book_data.php hash 中移除用户 ID 明文")
    print("=" * 60)
