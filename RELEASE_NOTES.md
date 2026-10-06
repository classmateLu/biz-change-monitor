# biz-change-monitor v0.1.0 (Experimental)

首个公开发布版本：通用业务对象历史变化监控引擎。

## 核心能力

- **纯函数 Diff 引擎**：5 类变化检测（new / removed / price_changed / name_changed / stores_changed），不依赖任何平台 SDK，可独立测试
- **防误报设计**：分页失败拦截、字段缺失不误判、价格数值化容错（"99.0" ≡ 99）、门店比较顺序无关
- **原子快照**：先写临时文件再替换，中断不留半文件；损坏快照自动跳过取次新
- **可配置完整性校验**：最小记录数 / 分页失败 / 详情失败比例，任一不过即终止（宁可停摆报警，不写错误数据）
- **写入幂等**：同日重跑默认跳过已成功写入的表，降低重复写入风险（at-least-once）
- **数据源可插拔**：实现一个 `DataSource.fetch()` 即接入新平台
- **钉钉集成（可选）**：多维表分批同步（表A 全量 / 表B 变化）+ 群机器人 Webhook 告警；不配置则 dry-run

## 快速开始

```bash
git clone https://github.com/YOUR_USER/biz-change-monitor.git
cd biz-change-monitor
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
python tests/test_diff.py   # Diff 引擎 12 项（另有钉钉同步安全 37 项 + 主流程安全 25 项，共 74 项）
python main.py              # 虚构数据 dry-run，无需任何账号
```

## 定位与边界

本仓库是**通用监控引擎**：以稳定业务 ID 为关联键，采集 → 快照 → 历史比对 → 变化事件 → 通知。

平台专属适配器不在本仓库内——接入你自己的数据源请实现 `adapters/base.py` 的接口。

⚠️ 本项目仅用于用户对自己有权访问的数据做自动化历史管理；不提供绕过登录、权限或安全机制的功能；不保证任何平台接口的长期兼容性。Meituan / DingTalk 等均为各自权利人的商标，本项目与其无关联。

## 说明

- Experimental：接口可能在 v0.2 前调整
- License: Apache-2.0
