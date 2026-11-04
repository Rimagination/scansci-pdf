# 闭源文献灰色源基准（20 篇）

固定 20 篇闭源论文，用来测试 ScanSci PDF 能不能从灰色源下到全文。清单、策略、输出都固定，方便不同时间、不同网络、不同版本之间对比。

## 清单

- 20 篇，覆盖 14 家出版商，1952–2021 年。
- 每篇都在 2026-11-04 用 Unpaywall 或 OpenAlex 确认 `is_oa=false`，没有 bronze / green / hybrid / OA 版本。
- DOI、标题、作者、年份都经 Crossref 核对。
- 完整元数据见 [`papers.json`](papers.json)，纯 DOI 见 [`dois.txt`](dois.txt)。

<details>
<summary><strong>展开 20 篇清单</strong></summary>

| # | DOI | 年份 | 出版商 | 论文 |
|---|---|---|---|---|
| 1 | `10.1038/nature14236` | 2015 | Springer Nature | Human-level control through deep reinforcement learning |
| 2 | `10.1038/nature16961` | 2016 | Springer Nature | Mastering the game of Go |
| 3 | `10.1109/JPROC.2004.840301` | 2005 | IEEE | The Design and Implementation of FFTW3 |
| 4 | `10.1109/TPAMI.2019.2913372` | 2020 | IEEE | Squeeze-and-Excitation Networks |
| 5 | `10.1145/1294261.1294281` | 2007 | ACM | Dynamo |
| 6 | `10.1145/1365815.1365816` | 2008 | ACM | Bigtable |
| 7 | `10.1145/1961189.1961199` | 2011 | ACM | LIBSVM |
| 8 | `10.1016/S0169-7552(98)00110-X` | 1998 | Elsevier | The anatomy of a large-scale hypertextual Web search engine |
| 9 | `10.1016/j.jbusres.2021.04.070` | 2021 | Elsevier | How to conduct a bibliometric analysis |
| 10 | `10.1007/BF00344251` | 1980 | Springer | Neocognitron |
| 11 | `10.1023/A:1009715923555` | 1998 | Springer | A Tutorial on Support Vector Machines |
| 12 | `10.1037/h0042519` | 1958 | APA | The perceptron |
| 13 | `10.1162/neco.2006.18.7.1527` | 2006 | MIT Press | A Fast Learning Algorithm for Deep Belief Nets |
| 14 | `10.1046/j.1365-2648.2003.02537.x` | 2003 | Wiley | The Delphi technique: myths and realities |
| 15 | `10.1098/rstb.1952.0012` | 1952 | The Royal Society | The chemical basis of morphogenesis |
| 16 | `10.1126/science.162.3859.1243` | 1968 | AAAS | The Tragedy of the Commons |
| 17 | `10.1086/228943` | 1988 | University of Chicago Press | Social Capital in the Creation of Human Capital |
| 18 | `10.1039/C8CS00699G` | 2019 | RSC | Toward practical solar hydrogen production |
| 19 | `10.1093/bioinformatics/btz307` | 2019 | OUP | FP2VEC |
| 20 | `10.1063/1.1699114` | 1953 | AIP | Equation of State Calculations by Fast Computing Machines |

</details>

## 结果（2026-11-04）

**20/20 成功，全部来自 `Sci-Hub(browser)`。**

- 总耗时 258.5 秒，单篇中位数 8.3 秒。
- 20 个 PDF 都校验过：`%PDF-` 头、页数、标题、作者都对得上。
- 实际镜像：`sci-hub.vg`。
- 逐篇结果见 [`results/2026-11-04_scihub_only.json`](results/2026-11-04_scihub_only.json)。

## 怎么跑

```bash
# 只测 Sci-Hub（推荐，结果最可比）
python benchmarks/grey-source-probe/run_probe.py

# 测三家灰色源
python benchmarks/grey-source-probe/run_probe.py --strategy grey_only

# 走 Tor
python benchmarks/grey-source-probe/run_probe.py --tor

# 只跑前 5 篇
python benchmarks/grey-source-probe/run_probe.py --limit 5
```

每次都会用新的状态目录，不会被缓存或机构会话影响。

也可以直接走 CLI（会混入 OA/机构通道）：

```bash
scansci-pdf batch benchmarks/grey-source-probe/dois.txt --scihub
```

## 怎么看结果

- 命中率就是成功下载并校验通过的比例；`source` 会写清楚实际来源。
- 失败不一定是论文不存在，可能是镜像、反爬、网络或地区问题。
- 这份清单高被引、经典论文多，所以容易 20/20。它更适合当回归测试：同环境下突然掉到 15/20，就说明通道出问题了。
- 它不是随机样本，不能拿来估计“闭源文献覆盖率”。
- 不同网络结果可能差别很大，比较时记下日期、版本、策略和是否走 Tor。

## 注意

- 灰色源在部分辖区可能不合法，请只在你有权测试的环境里用。
- 只记录“能不能下到”，不构成再分发授权；不要提交或传播下载的 PDF。
- `runs/` 和 `*.pdf` 已 gitignore。
- 如果配置了 `legal_only` 或 `scihub_enabled=false`，不要用这个基准绕过它。
