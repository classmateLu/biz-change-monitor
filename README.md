# biz-change-monitor

业务对象历史变化监控引擎 —— 以稳定业务 ID 为关联键，持续采集 → 快照 → 历史比对 → 变化事件 → 通知/多维表 → 历史查询。

> 适用场景：任何"ID 稳定、属性会变"的业务对象——商品价格、套餐内容、适用门店、库存状态、SaaS 数据、API 返回值……
> 本仓库附带一个**虚构数据**的示例适配器，克隆即可跑通全链路，无需任何账号。

## 核心流程

```text
平台数据源 (adapter)
    ↓ 归一化 {gid, name, price, ...}
完整性校验 (validation) ── 失败 → 告警退出，不产生任何写入
    ↓
原子快照 (snapshot: JSON)
    ↓ 与最近有效基线比对
变化检测 (diff: 纯函数)  → new / removed / price_changed / name_changed / stores_changed
    ↓
通知与同步 (钉钉多维表 / Webhook) + 历史查询
```

## 特性

- **纯函数 Diff**：不依赖浏览器/网络/平台 SDK，可独立测试
- **防误报设计**：分页失败拦截、字段缺失不误判、价格数值化容错（"99.0"≡99）、门店比较顺序无关
- **原子快照 + 容错读取**：中断不留半文件，损坏文件自动跳过
- **写入幂等**：当天重跑自动跳过，杜绝重复记录
- **数据源可插拔**：实现一个 `DataSource.fetch()` 即接入新平台
- **钉钉集成可选**：不配置则 dry-run，配置后自动同步多维表 + Webhook 告警

## 快速开始

```bash
git clone https://github.com/YOU/biz-change-monitor.git
cd biz-change-monitor
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# 跑测试
python tests/test_diff.py

# 跑一次示例流程（虚构数据，dry-run 模式）
python main.py
```

## 接入你的数据源

1. 在 `adapters/` 下新建 `my_adapter.py`，继承 `DataSource` 并实现 `fetch()`：

```python
from .base import CollectionResult, DataSource
from core.models import make_item

class MyAdapter(DataSource):
    name = "my_platform"

    def fetch(self) -> CollectionResult:
        items = [make_item("ID-001", name="项目A", price=99.0, count=1)]
        stores = {"ID-001": ["门店1", "门店2"]}
        return CollectionResult(items=items, stores=stores, total=len(items),
                                failed_pages=[])
```

2. 在 `main.py` 里把 `ExampleAdapter` 换成你的适配器。

**去资产化原则**：平台接口地址、参数结构、登录态处理等若涉及公司内部系统或商业平台，请保持在私有环境，不要提交到公共仓库。

## 钉钉同步（可选）

1. 钉钉开放平台创建企业内部应用，获取 AppKey/AppSecret；创建多维表（表A 全量 / 表B 变化）
2. `cp config/config.example.yaml config/config.yaml` 并填入配置（或使用 `.env`）
3. 配置不齐时自动 dry-run；配置齐备后每次运行同步表A/表B（50 行/批、幂等保护、失败 Webhook 告警）

## 定时运行

`deploy/launchd/` 提供 macOS launchd 模板；Linux 用 cron / systemd timer 执行同一条命令即可。

## 测试

```bash
python tests/test_diff.py        # 直接运行
pytest tests/                    # 或 pytest
```

覆盖：新增/删除/价格/名称/门店变化、多字段同变、零误报、数值容错、字段缺失、基线缺失跳过门店比对。

## 安全与合规声明

- 本项目定位为**用户对自己有权访问的数据**做自动化历史管理
- 使用者需自行确认目标平台的服务条款，遵守适用法律法规
- 本项目**不提供**绕过登录、权限或安全机制的功能；不内置任何平台专属适配器
- 不保证任何平台内部接口的长期兼容性
- Meituan / DingTalk（钉钉）等均为各自权利人的商标，本项目与其无关联

## License

Apache-2.0（见 [LICENSE](LICENSE)）

## Roadmap

- [ ] v0.2：查询机器人框架移植（钉钉 Stream，管理员白名单 + 审计日志）
- [ ] v0.2：更多通知渠道（飞书 / Telegram / 邮件）
- [ ] v0.3：SQLite 存储后端可选；Web 查看面板
