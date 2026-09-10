from __future__ import annotations


def safe_ai_error_message(
    exc: Exception,
    *,
    provider: str,
    action: str,
) -> str:
    """Return actionable cloud-AI diagnostics without echoing provider text."""

    status_code = getattr(exc, "status_code", None)
    if status_code is None:
        status_code = getattr(getattr(exc, "response", None), "status_code", None)
    try:
        status = int(status_code) if status_code is not None else None
    except (TypeError, ValueError):
        status = None

    compatible = provider == "openai_compatible"
    service = "OpenAI 兼容服务" if compatible else "OpenAI"
    if status == 401:
        guidance = (
            "请核对服务地址、该服务商的 API Key 与模型名称是否属于同一个平台。"
            if compatible
            else "请在“连接与 API”中重新填写由 OpenAI Platform 创建且仍有效的 API Key。"
        )
        return f"{service} 身份验证失败（401），本次{action}未调用成功。{guidance}"
    if status == 403:
        return f"{service} 拒绝访问（403）。请检查账户权限、项目权限或服务地区后重试。"
    if status == 404:
        return f"{service} 未找到请求的接口或模型（404）。请核对模型名称与 API 地址。"
    if status == 429:
        return f"{service} 返回额度不足或请求过快（429）。请检查余额、配额和速率限制。"
    if status is not None:
        return f"{service} 请求失败（HTTP {status}），本次{action}未调用成功；请稍后重试或检查连接设置。"
    return f"{service} 连接失败（{type(exc).__name__}），本次{action}未调用成功；请检查网络和连接设置。"
