PYTHON ?= python

.PHONY: doctor test stats probe-rss background help

help:
	@echo "make doctor      检查环境与数据库"
	@echo "make test        运行全部本地测试"
	@echo "make stats       查看下载数据库统计"
	@echo "make probe-rss   小规模真实测试一个固定 RSS"
	@echo "make background  为已下载音频补齐背景信息与公开资产"

doctor:
	$(PYTHON) doctor.py

test:
	PYTHON_BIN=$(PYTHON) bash scripts/test.sh

stats:
	$(PYTHON) main.py stats

probe-rss:
	$(PYTHON) probe.py --source podcast_rss --feeds 1 --episodes 5

background:
	$(PYTHON) main.py background --limit 10000 --workers 4 --background all
