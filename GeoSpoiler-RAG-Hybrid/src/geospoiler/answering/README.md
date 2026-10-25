# answering

query.py выбирает основной и резервный маршруты; late_fusion.py объединяет поиски; card_context.py готовит контекст; reference_hints.py и answer_postprocess.py обрабатывают ссылки/выдачу; composer.py обслуживает search. LLM-synthesis пока остаётся внутри этих профильных модулей без дополнительной косметической разбивки.
