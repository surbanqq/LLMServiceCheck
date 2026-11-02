# LLMServiceCheck

面向自部署大语言模型服务的轻量上线前检查工具。

[English](readme.v1.md)

通过兼容 OpenAI 协议的服务接口，测量延迟、吞吐、请求错误和前缀缓存复用，检查聊天接口行为。可使用一个或多个外部参考文件进行比较。项目不内置特定厂商基准，也不自动给出准入结论。

| 子命令 | 测试内容与目的 |
| --- | --- |
| `prepare` | 使用本地分词器生成指定输入长度的数据及可选缓存数据对，为后续测试准备输入。 |
| `baseline` | 按输入长度测量延迟、吞吐和请求错误，可与外部参考文件比较。 |
| `capacity` | 在指定输入长度下逐档测试闭环并发，观察吞吐、延迟和错误随并发的变化。 |
| `cache` | 先预热再等待指定时间发送探测请求，检查前缀缓存复用及延迟变化。 |
| `compat` | 检查非流式与流式响应、用量、JSON、工具调用和非法参数处理。 |

## 环境与安装

- Linux，Python 3.10 及以上。
- 能访问待测模型服务。客户端不需要模型权重、PyTorch 或 GPU。
- 只有 `prepare` 需要分词器相关依赖，以及与服务一致的本地分词器和聊天模板文件。

Linux：

```bash
bash install.sh
source .venv/bin/activate
```

也可以手动安装：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

如果只运行已生成的数据，使用 `bash install.sh --runner-only`，可跳过分词器依赖。下文的 `python` 均指虚拟环境中的解释器。

## 配置服务

复制示例配置为 `configs/local.json`：

```bash
cp configs/example.service.json configs/local.json
```

参考基准值和延迟上限通过独立的 `--reference` 文件提供。

- `base_url` 填服务接口前缀，例如 `http://127.0.0.1:8000/v1`；脚本自动追加 `/chat/completions`。
- `model` 填服务实际接受的模型名称。
- `max_context_tokens` 填实际部署的上下文上限。
- 密钥通过环境变量传入；无鉴权服务可设置 `require_api_key: false`。

```bash
export LLM_API_KEY='your-key'
```

| 配置 | 含义 |
| --- | --- |
| `max_context_tokens` | 实际部署的上下文上限，包含输入和输出预算 |
| `lengths` | `prepare` 生成数据的输入词元长度列表；可用 `--lengths` 覆盖 |
| `max_tokens`、`min_completion_tokens` | 请求的输出上限，以及有效计时样本的最小输出量 |
| `timeout_s`、`read_timeout_s` | 请求总时限、套接字操作超时；示例分别为 900 秒和 300 秒 |
| `length_tolerance` | 目标输入长度与服务端计数的相对误差容忍度 |
| `require_cold_cache` | 是否排除命中缓存或未报告缓存计数的基线样本，默认 `true` |
| `allow_reasoning` | 是否允许带思考输出的计时样本，默认 `false` |
| `request_extra`、`template_options` | 服务请求扩展参数，以及本地分词时匹配的模板参数 |
| `thinking_variants` | 可选的思考控制兼容性探测参数；参数被接受不代表实际行为已验证 |

`allow_reasoning` 只控制样本筛选，不负责开关模型思考。思考开关应使用服务支持的请求参数。对于支持 `chat_template_kwargs.enable_thinking` 的服务，可设置：

```json
"request_extra": {"chat_template_kwargs": {"enable_thinking": false}},
"template_options": {"enable_thinking": false}
```

生成数据与运行测试时应保持思考模式、聊天模板一致。分词器通常包含 `tokenizer.json` 和 `tokenizer_config.json`；部分模型还需要词表或单独的模板文件。

## 生成测试数据

`--tokenizer` 指向本地目录，其中需包含与待测模型服务一致的分词器和聊天模板。可从部署模型目录复制，也可从 Hugging Face 下载对应模型版本的文件。例如，仅下载 Qwen3.8-27B 的分词器相关文件，无需下载模型权重：

```bash
python -m pip install -U huggingface_hub
hf download Qwen/Qwen3.8-27B \
  tokenizer.json tokenizer_config.json chat_template.jinja merges.txt \
  --local-dir ./models/tokenizer
```

`prepare` 只读取本地目录，不会自动下载文件。

```bash
python benchmark.py prepare --config configs/local.json \
  --tokenizer ./models/tokenizer \
  --samples 10 --cache-type prefix-replace --cache-tokens 70000 \
  --out workloads/run01
```

输入长度读取配置文件的 `lengths`，可用 `--lengths` 覆盖；均未设置时使用默认值 `4000 8000 16000`。列表不能为空，各项必须是不重复且不小于 1000 的整数。

生成 `prompts.jsonl`、`workload_manifest.json`；指定缓存模式时，同时生成 `cache_pair.json`。清单记录本次实际使用的长度。输入长度是**包含聊天模板的词元数量**，不是字符数。输入长度加 `max_tokens` 必须小于等于实际部署的上下文上限。应按真实业务长度和对外承诺的服务范围选择测试点。后续 `baseline` 按生成数据中的长度分组测试，无需再次指定长度。缓存数据对的长度仍由 `--cache-tokens` 单独控制。

| `--cache-type` | 生成方式 |
| --- | --- |
| `none`，默认 | 不生成缓存数据对 |
| `prefix-replace` | 约 90% 相同词元前缀，替换末尾文本 |
| `message-append` | 完整保留预热消息，追加助手与用户消息，使相同词元前缀约占探测输入的 90% |

`--cache-tokens` 为探测请求的近似输入长度，默认 70000。预热和探测输入加各自输出预算均须适配上下文。文件记录本地词元数和核验后的相同前缀长度；缓存等待时间在运行缓存检查时指定。

每次冷缓存基线测试应重新生成数据，使用新目录；默认自动生成新的运行标识。更换分词器、聊天模板或思考配置后也应重新生成。

## 执行测试

### 接口兼容性检查（`compat`）

检查结构化输出、工具调用、用量字段、流式响应和非法参数处理。

```bash
python benchmark.py compat --config configs/local.json --out results/compat01
```

增加 `--negative-auth` 可使用故意错误的密钥检查认证失败处理。

### 基线性能测试（`baseline`）

只输出实测数据：

```bash
python benchmark.py baseline --config configs/local.json \
  --prompts workloads/run01/prompts.jsonl --concurrency 10 --requests 10 \
  --out results/baseline01
```

通过重复指定 `--reference`，与多个参考文件比较。开始下一轮冷缓存测试前，先重新执行 `prepare`，将输出改为 `--out workloads/run02`：

```bash
python benchmark.py baseline --config configs/local.json \
  --prompts workloads/run02/prompts.jsonl --concurrency 10 --requests 10 \
  --reference references/demo-a.json --reference references/demo-b.json \
  --out results/baseline02
```

`demo-a.json` 为 Demo A 参考值，`demo-b.json` 为 Demo B 参考值。Demo A 没有 200000 及以上输入词元的区间，因此 224000 显示 `NOT_APPLICABLE`；Demo B 覆盖 configs/example.service.qwen3.8-27b.json配置的全部六个长度。格式和来源见[参考文件说明](docs/REFERENCES.md)。脚本在执行时读取文件，并将完整快照存入 `report.json`；修改参考文件只影响后续运行。

### 前缀缓存检查（`cache`）

使用已生成的缓存测试对预热前缀，等待指定时间后发起探测，观察缓存复用和延迟变化。

```bash
python benchmark.py cache --config configs/local.json \
  --pair workloads/run01/cache_pair.json --wait-seconds 30 --out results/cache01
```

### 并发容量测试（`capacity`）

在 `--length` 指定的输入长度下逐档增加并发，观察负载增加时的吞吐、延迟和错误情况。

```bash
python benchmark.py capacity --config configs/local.json \
  --prompts workloads/run01/prompts.jsonl --length 16000 \
  --concurrency 1 2 4 8 --duration 60 --allow-reuse --out results/capacity01
```

容量测试采用闭环并发，`--duration` 指定每档持续时间。后续并发档位会复用之前的输入；`--allow-reuse` 还允许同一档位复用输入，可能命中缓存。不指定该选项时，每档用完独立输入即结束。持续时间到达后停止发起新请求，已发出的请求可继续执行到完成或超时。

## 阅读结果

- `report.md`：每个输入长度一张表，包含实测值，以及各参考的基线、上限、实测与基线之比、实测与上限之比；保留成功数、有效数、错误率和比较条件说明。
- `report.json`：完整配置、参考文件快照、全部成功请求统计、有效子集统计及吞吐。
- `requests.jsonl`：逐请求的计时、用量、错误和请求标识；不保存输入输出正文和工具参数。

主表只统计有效样本。默认要求输入长度符合容忍度、输出足够长、缓存计数明确且为零、流式计时可用、未观察到思考输出。失败和被排除的请求仍计入请求数量。部分成功可能隐藏最慢的请求，子集倍数需要结合错误率阅读；少量样本也不足以稳定估计尾延迟。

TTFT（Time to First Token，首词元延迟）从客户端连接前开始计时，到首个正文、思考或工具增量事件为止；`first_content_ms` 单独记录首个可见正文。TPOT（Time per Output Token，平均输出词元耗时）按“流结束时间减首个生成事件时间，再除以输出词元数减一”计算，包含流结束开销，不是逐词元间隔测量。E2E（End-to-End latency，端到端延迟）覆盖完整请求。百分位采用最近秩法。

`avg` 表示算术平均值，`p50` 表示中位数，`p90` 表示第 90 百分位。套接字操作使用读取超时和总超时中的较小值；流式心跳可能在没有生成词元时维持连接，但请求总时限仍然生效。超时配置与参考文件中的延迟上限是独立的设置。

`combined_tpm_observed` 为实测的输入加输出 TPM（Tokens per Minute，每分钟词元数），不是账户配额。

倍数大于 1 表示延迟更长。比较状态：

| 状态 | 含义 |
| --- | --- |
| `COMPARABLE` | 参考文件声明的可检查条件已满足 |
| `LIMITED` | 存在失败、样本不足或条件差异，倍数只供描述性参考 |
| `NO_VALID_SAMPLES` | 无有效计时样本 |
| `NOT_APPLICABLE` | 没有匹配的输入长度区间 |

缺少区间或指标时显示 `-`，不进行外推。条件匹配不代表硬件、模型质量和工作负载难度完全一致。

兼容性报告列出检查项、结果和判定条件。`PASS` / `FAIL` 表示本次执行是否满足该项条件，不代表完整功能已验证；`NOT_TESTED` 表示本次未执行；`MANUAL` 表示脚本未自动验证，需要另行验证。参数请求被接受不代表参数实际生效。运行状态 `COMPLETED` 仅表示执行完成，不代表所有检查通过。

缓存命中率为服务端返回的缓存输入词元数除以探测输入词元数。预热应使用新的前缀；单次预热和探测不能证明缓存存活时间或并发下的行为。

退出码：`0` 表示执行结束并生成报告，即使有请求错误；`2` 表示配置、文件或程序错误；`130` 表示中断。自动化流程应读取报告里的错误数量和比较状态。数据和结果输出目录必须是新目录。

## 本地验证

```bash
python -m unittest discover -s tests -q
python benchmark.py --help
python benchmark.py prepare --help
```

测试使用本地模拟服务和分词器夹具，用于验证脚本行为，不代表实际模型性能。生成数据是合成数字文本，正式上线前还应补充真实应用工作负载。

可查看[数据的报告示例](docs/SAMPLE_REPORT.md)和[本地验证记录](docs/VERIFICATION.md)。
