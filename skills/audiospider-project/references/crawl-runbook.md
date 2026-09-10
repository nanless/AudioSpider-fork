# Unified collection runbook

```bash
cd /root/code/github_repos/AudioSpider-fork
source /root/miniforge3/etc/profile.d/conda.sh
conda activate audiospider
python doctor.py
```

Start bounded. Collection writes metadata jobs; download writes large files.

```bash
python probe.py --source bilibili --keywords "人物访谈 长视频" --search-pages 1 --videos 1 --parts 1
AUDIOSPIDER_YOUTUBE_MAX_ITEMS=1 python probe.py --source youtube --videos 1
python collect.py --spiders bilibili
AUDIOSPIDER_YOUTUBE_MAX_ITEMS=1 python collect.py --spiders youtube
python main.py --source bilibili --artifact-kind video_bundle --limit 1 --workers 1
python main.py --source youtube --artifact-kind video_bundle --limit 1 --workers 1
python scripts/audit_media_queue.py
```

If disk is below the configured reserve, stop. Do not lower the reserve merely to force a run.
Use `--allow-bilibili-cookie` only after explicit user authorization; otherwise ambient cookies are ignored.
