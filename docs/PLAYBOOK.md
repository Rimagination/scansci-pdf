# ScanSci-PDF 反爬作战手册（PLAYBOOK）

> **给每个接手本项目的 Agent 和开发者**：反爬相关代码的机制、位置、参数、历史教训全部记录在这里。
> 这个项目反复出现过"同一类问题在不同版本重造轮子"的事故，根源是会话失忆 + 造轮子前没搜家底。
> **修改任何反爬相关代码前，先读完这一份。**

## 铁律（每条都对应一次真实事故）

1. **造轮子前先 grep 家底。** 镜像健康存储先后被造过三代（探测系统 → domain_db → 内存 dict），三代平行存在的维护成本远高于合并。
2. **修复必须变成测试。** 没有测试的修复在下一个会话里等于不存在。
3. **修复必须发布。** 不发 PyPI，用户手里的永远是旧版（"我这里好好的，用户那里不行"的根源之一）。
4. **双 checkout 必须同步。** 编辑在 `D:\scansci-pdf`，运行在 `C:\Users\Liang\plugins\scansci-pdf`。`tests/test_checkout_sync.py` 会拦——它已抓出 268 文件漂移和多次单文件遗漏。
5. **多文件 cp 必须用显式目标路径。** `cp a b dest/` 曾三次把 `sources/` 下的文件错误复制到平面层（同步测试抓的）。
8. **用户数据永不进仓库树。** 用户的文献清单、DOI 集合、state JSON、下载产物只存在于 Downloads/数据目录——仓库是公开的，`git add -A` 前先想一遍今天动了什么。发布后跑一遍清单特征 grep（2026-08-31 审计模式：4 渠道全查）。
6. **Playwright sync 对象线程亲和。**（2026-08-31：退出收割时跨线程 close 失败 + Windows 杀父不杀子，4 个 chrome 残留）
7. **每个验证过的单元立即提交。**（2026-08-31：一条手滑的 cp 覆盖了未提交的 progress_reporter.py，靠 git 恢复——攒着就是风险） 退出收割时跨线程 close() 必然失败，Windows 上杀父进程不杀子进程——收割必须 tree-kill（taskkill /F /T）。

## 镜像健康：唯一权威存储

**所有验证墙/结构性冷却状态存于 `domain_db.py` 的 `wall_state` 表**（`domain_stats.db`，SQLite + WAL），由 `scihub.py` 的 `_wall_guard / _wall_pace / _note_wall / _note_wall_success / _note_structural` 读写。**不要再造平行的内存 dict 或 JSON 健康存储。**

- 连接模式是"每操作开闭"——不要改回线程本地长连接（Windows 文件锁会让调用方的临时目录清理失败）。
- 剪枝条件用 `last_solve`（一天未动），**不要用 `cooldown_until`**（0 是合法的健康态）。

## 结构性失败签名表（2026-08-31 实测，会过期）

| 响应签名 | 判定 | 处理 |
|---|---|---|
| `challenges.cloudflare.com/turnstile` 或标题 `Verification - Sci-Hub` | Turnstile 交互门 | 可见浏览器→人工点一次模式；无头→结构性冷却 2h |
| 请求 `/DOI` 却返回标题含 `search proxy to download` 的首页 | 镜像壳坏死 | 结构性冷却 2h |
| `你是机器人吗` / `altcha` | ALTCHA 墙 | 自动求解（点"不是"→等 PoW≈17s→重载）+ 25s 验证节奏；解后仍在墙→指数冷却 |
| `iframe` / `/downloads/` / `embed` | 正常文章页 | 正常提取下载 |
| `не найден` / `article not found` | 未收录 | 换源，不重试 |

## 节奏参数（`sources/scihub.py`，全部持久化于 wall_state 表）

- `_WALL_MIN_SPACING_SEC = 25`：两次验证求解的最小间隔
- `_WALL_COOLDOWN_BASE_SEC = 90`，×3 指数递增，cap 900：撞墙后冷却
- `_MIRROR_STRUCTURAL_COOLDOWN_SEC = 7200`：结构性坏死跳过 2 小时

## Elsevier API 节奏（2026-08-31 实测）

按**请求速度**限流：批量 1.6 篇/秒时触发瞬时失败（64 篇失败中 56 篇为瞬时限流）；
**15s/篇 的节奏实测 56/56 全部收回**。HEAD 探测（200=有权）不占配额、可随时做。
冷却重试用 `_transient_retry`（fast_retry_delay_sec=15, workers=1）。**换 IP 无增益**：
权限矩阵显示代理与直连双路由 99.7% 等价（1456/1460 ENTITLED）。

## 按域直连

sci-hub.ru 按**出口 IP** 限速：代理用户共享一个出口，很快被墙；直连每用户独立 IP，清白（2026-08-30 实测：同机代理=常驻墙，直连=干净文章页）。`network.select_proxy_for_url` 默认让 `sci-hub.ru` 直连——`direct_domains` 扩展名单、`scihub_direct: false` 关闭、Tor 优先级最高。

## 浏览器生命周期

- **常驻工作池**：`sources/scihub.py` 的 `_race_pool`（线程数 = `scihub_browser_workers`），整批共享浏览器，禁止在每篇论文的 finally 里关浏览器（历史事故：闪窗 + 丢弃 Cloudflare 通行证）。
- **退出收割**：`browser_engine._reap_browsers_at_exit`（atexit）——优雅关闭失败则 tree-kill。
- **无头开关**：`scihub_browser_headless` 只影响竞速浏览器；Turnstile 人工点一次需要它为 false。

## SI 附件

`supplementary.py`；批量开关 `download_si`（默认关）；成功论文抓 SI 存 `{DOI}_SI{n}.{ext}` + `si_manifest.json`。**灰色源没有 SI**（资产模型 = 正文单 PDF），SI 只在出版商页面——所以 SI 命中率与出版商反爬强度相关，与正文下载能力相关但不同步。浏览器兜底走常驻池。

## 已知镜像状态（2026-08-31 实测，必然过期）

| 镜像 | 状态 |
|---|---|
| sci-hub.ru | 活，ALTCHA 可自动解 |
| sci-hub.vg / .ren / .ee | 活，Turnstile 交互门（人工点一次模式） |
| sci-hub.al / .mk | 坏（回首页壳） |
| sci-hub.se / .st | 代理不可达（SSL） |
| sci-hub.shop | 不是镜像，是镜像目录站（只列国际家族，价值有限） |

## 待办 / 已知边界

- Turnstile 人工点一次已实现，但需 `scihub_browser_headless=false` 才能生效
- ALTCHA 吞吐：ru 服务端限速有记忆效应，`_WALL_*` 参数可继续调
- 结构性失败签名是正则匹配，新型反爬页面可能漏判——发现新签名请加进 `_classify_mirror_page`
