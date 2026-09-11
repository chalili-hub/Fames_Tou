"""OAuth2 授权 CLI — 首次使用必须跑一次。

用法:
    python -m ml_research.authorize

流程:
    1. 先到 https://developers.mercadolibre.com.mx 创建应用,
       拿到 CLIENT_ID / CLIENT_SECRET,把 REDIRECT_URI 配进应用
    2. 填入 ml_research/.env(模板见 .env.example)
    3. 运行本命令 → 浏览器打开授权页 → 授权后回调地址带 ?code=xxx
    4. 把 code 粘回终端 → token 落盘到 data/ml_tokens.json
"""
import sys


def main():
    from . import config
    from .ml_api import TokenManager, MlApi, TokenError

    if not config.CLIENT_ID or not config.CLIENT_SECRET:
        print("[授权] 缺少 MLM_CLIENT_ID / MLM_CLIENT_SECRET")
        print("        1. 在 https://developers.mercadolibre.com.mx 创建应用")
        print("        2. 复制 ml_research/.env.example 为 .env 并填写")
        sys.exit(1)

    tm = TokenManager()
    url = tm.get_authorize_url()
    print("=" * 64)
    print("1) 在浏览器打开下面链接并完成授权:")
    print("   ", url)
    print("=" * 64)

    code = input("\n2) 授权后浏览器会跳转到回调地址,\n"
                 "   把 URL 中 code= 后面的值粘贴到这里: ").strip()
    if not code:
        print("[授权] 未输入 code,退出")
        sys.exit(1)

    try:
        tm.exchange_code(code)
    except TokenError as e:
        print(f"[授权] 失败: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"[授权] 网络/服务端错误: {e}")
        sys.exit(1)

    api = MlApi(tm)
    try:
        me = api.get_me()
        print(f"\n[授权] 成功!user_id={me.get('id')}  nickname={me.get('nickname')}")
        print(f"[授权] access_token 有效期 6h,自动续期;token 已保存到 {tm.token_file}")
    except Exception as e:
        print(f"\n[授权] token 已保存,但校验失败: {e}")


if __name__ == "__main__":
    main()
