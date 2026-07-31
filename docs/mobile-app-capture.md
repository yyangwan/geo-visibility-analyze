# Mobile App Capture

Platform audit responses can be collected from the consumer Android apps
instead of the vendors' model APIs. The rest of the audit pipeline continues
to consume the existing `PlatformResponse` contract.

## Collection mapping

| Audit platform | Gateway platform | Android app |
| --- | --- | --- |
| `deepseek` | `deepseek` | DeepSeek |
| `doubao` | `doubao` | 豆包 |
| `hunyuan` | `yuanbao` | 腾讯元宝 |
| `qwen` | `qwen` | 千问 |
| `kimi` | `kimi` | Kimi |

The mobile adapter preserves `response_text`, `citations`, latency,
`raw_response`, `search_metadata`, and `request_params`. Citation records keep
the original `url`, `title`, and `domain` keys and may also contain
`site_name`, `index`, `url_resolution`, and collection status.

Set `AISCOPE_MOBILE_APP_CAPTURE_ENABLED=false` to return audit collection to
the original platform API adapters. Internal LLM calls used for prompt
generation, analysis, and suggestions are not affected by this switch.
Product-website AI citation checks use the same mobile collection switch.

## TODO: Yuanbao exact source URLs

Yuanbao currently exposes every reference's site name, title, and snippet but
does not expose the hidden destination path through accessibility, Activity
extras, logcat, or a debuggable WebView socket.

- [ ] Prepare a dedicated rooted Android test device.
- [ ] Add an authorized Frida probe for `WebView.loadUrl`,
      `WebViewClient.shouldOverrideUrlLoading`, `Intent.setData`, and network
      request construction.
- [ ] Correlate captured URLs and redirect chains with the Appium reference
      index.
- [ ] Store `original_url`, `redirect_chain`, and `final_url`.
- [ ] Keep inferred title-search matches explicitly labeled and separate from
      observed URLs.
