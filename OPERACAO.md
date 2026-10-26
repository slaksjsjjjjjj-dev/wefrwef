# Operação e diagnóstico

## Rodada manual recomendada para o primeiro teste

Comece com 0, 6 e 12 horas, e confirme os três provedores antes de ativar os 29 horários até F168:

```bash
python -m sideral_multimodels run --date AAAAMMDD --cycle 00 --hours "0,6,12"
```

## Checklist do primeiro resultado

- `manifest.json`: data/ciclo e grade de 0,5° conferidos.
- `qc.json`: ECMWF, GFS e ICON aparecem de fato nos campos com dados.
- `consensus.nc`: coordenadas `latitude` (361) e `longitude` (720), com valores plausíveis e unidades conhecidas.
- `tp6h`: confirme `GRIB_startStep`, `GRIB_endStep` ou `GRIB_stepRange` para cada origem e compare manualmente as janelas de precipitação.
- `plots/`: confirme que as prévias não estão totalmente vazias nem invertidas.
- Workflow: confirme tamanho e duração do artefato antes de configurar consumidores externos.

## Regras de segurança operacional

- Não inclua segredos em arquivos, logs, commits ou capturas de tela.
- Não trate a concordância entre modelos como probabilidade calibrada.
- Não combine precipitações acumuladas em janelas temporais diferentes.
- Não anuncie publicação operacional até validar uma rodada completa e persistir QC.
- Se qualquer um dos três modelos não puder ser baixado/processado, o produto completo não é publicado.
