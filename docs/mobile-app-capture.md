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

## Yuanbao exact source URLs

Yuanbao source URLs are collected from the app's own source-detail workflow:

1. Open the answer's source panel and enumerate every reference from Android's
   native accessibility hierarchy.
2. Open each reference and select the detail page's top-right menu.
3. Focus the Huawei share sheet's `Copy Link` action with hardware-key
   navigation and activate it. The share sheet blocks shell touch injection,
   so the focused container is verified against its `Copy Link` descendant.
4. Read and decode the copied URL through Appium Settings' clipboard receiver.
5. Return to the source panel and continue until every reference is collected.

Yuanbao uses a pure ADB path after launch because UiAutomator2 can deadlock on
its Compose answer view. Native hierarchy dumps and binary `adb exec-out`
transfers are retried to tolerate transient device idle and transport errors.
If an installed Android app claims a source App Link, the gateway temporarily
disables that package, reopens the reference through Yuanbao's web flow, and
restores the package immediately afterward.

Only observed HTTP(S) clipboard values are emitted with
`url_resolution="exact"`; failed records retain their collection status and
error message rather than receiving an inferred URL.
