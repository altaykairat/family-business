# Ручной пакет

PREVIEW: это НЕ R-base; исходный источник sa_v1:sonnet.

Используйте ту же фактическую модель/версию, что в baseline, и новый чат на каждый промпт. Агент claude — имя почтового ящика, а не проверенное имя модели. python3 scripts/paste.py status --run variants/codex/runs/sprint1-robustness-neutral_swap-v1

После ручного переноса: python3 -m variants.codex.packets ingest --run variants/codex/runs/sprint1-robustness-neutral_swap-v1
Сырые ответы и исходная оценка сохраняются. Повторная оценка — экспериментальный кандидат, не решение апелляции.
