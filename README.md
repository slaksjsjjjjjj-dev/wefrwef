# SIDERAL MULTIMODELS 0.5

Protótipo independente da Sideral Meteorologia para combinar **três modelos numéricos** em uma grade global comum de **0,5°**:

- ECMWF IFS Open Data
- NOAA GFS
- DWD ICON Global

A implementação é própria e não contém código copiado da Sigma. O produto é inspirado funcionalmente em sistemas multimodelo, mas usa um método simples, determinístico e auditável, não uma réplica do algoritmo interno de terceiros.

## Estado da versão

**v0.1.0 — protótipo técnico.** O pacote contém adaptadores de download, leitura GRIB2, interpolação para 0,5°, cálculo de média, spread, quantidade de fontes e índice descritivo de concordância, arquivo NetCDF, manifesto JSON, QC e imagens PNG de prévia.

A execução operacional com dados recentes ainda precisa ser validada de ponta a ponta nos três provedores. Os modelos podem publicar em horários, horizontes e acumulações de precipitação diferentes; dados ausentes são marcados como NaN, nunca inventados.

## Estrutura

```text
sideral-multimodels-05/
├── .github/workflows/multimodels-05.yml
├── config.yml
├── .env.example
├── pyproject.toml
├── requirements.txt
├── src/sideral_multimodels/
│   ├── cli.py
│   ├── core.py
│   ├── processing.py
│   └── providers/downloaders.py
├── tests/
└── docs/
```

## Saídas

Para cada rodada, os produtos serão gravados em `output/YYYYMMDDHH/`:

- `consensus.nc`: campos na grade global de 0,5°;
- `manifest.json`: rodada, grade, variáveis, fontes e metadados;
- `qc.json`: contribuição das fontes por variável e hora;
- `plots/`: prévias PNG para temperatura a 2 m e precipitação em 6 horas, quando houver dados válidos.

| Campo | Unidade | Significado |
|---|---|---|
| `t2m` | °C | Temperatura a 2 m |
| `d2m` | °C | Ponto de orvalho a 2 m |
| `u10`, `v10` | m/s | Componentes do vento a 10 m |
| `msl` | hPa | Pressão ao nível médio do mar |
| `tp6h` | mm | Precipitação no intervalo de 6 h, quando os metadados permitem comparação |
| `*_spread` | unidade do campo | Desvio padrão entre modelos |
| `*_nmodels` | quantidade | Modelos com valores finitos em cada célula |
| `*_agreement` | % | Índice descritivo baseado no spread, não uma probabilidade calibrada |

A média do vento é calculada nas componentes U/V; velocidade e direção podem ser derivadas delas. A precipitação exige cuidado especial: o processador só calcula `tp6h` se conseguir identificar uma janela temporal compatível; caso contrário, deixa o campo ausente.

## Instalar e executar no Windows

Recomendado: Python 3.11 de 64 bits. O ecCodes/GRIB precisa de bibliotecas compatíveis com a plataforma. Dentro da pasta extraída, no PowerShell ou CMD:

```powershell
py -3.11 -m venv .venv
.venv\Scripts\activate
python -m pip install --upgrade pip
python -m pip install -e ".[test]"
python -m sideral_multimodels doctor
python -m pytest -q
```

Para uma primeira rodada curta:

```powershell
python -m sideral_multimodels run --date 20261026 --cycle 00 --hours "0,6,12" --raw-dir data/raw --output-dir output
```

Sem `--date` e `--cycle`, o sistema seleciona uma rodada UTC usando a margem de disponibilidade definida em `config.yml`. Essa margem é uma heurística e não garante que os três provedores já tenham publicado todos os campos.

Comandos disponíveis:

```powershell
python -m sideral_multimodels doctor
python -m sideral_multimodels download --date 20261026 --cycle 00 --hours "0,6,12"
python -m sideral_multimodels process --date 20261026 --cycle 00 --hours "0,6,12"
python -m sideral_multimodels run --date 20261026 --cycle 00
```

A primeira versão exige hora 0 para normalizar acumulações de precipitação.

## GitHub Actions

O workflow `.github/workflows/multimodels-05.yml` oferece execução manual e agendada aproximadamente às 01Z e 13Z, mirando ciclos 00Z/12Z para favorecer o horizonte comum. Ele instala bibliotecas GRIB, executa testes, baixa os três modelos, processa o consenso e guarda os resultados como artefato por 14 dias. A saída não é commitada numa branch de dados, evitando crescimento excessivo do histórico Git com arquivos meteorológicos grandes.

O projeto não exige Cloud Run e não altera outros pipelines ou branches do Sideral. Antes de operar regularmente, monitore disponibilidade, duração, limites dos serviços e tamanho dos artefatos.

## Método de combinação

1. Baixa os campos comuns disponíveis no ECMWF, GFS e ICON.
2. Lê metadados GRIB2 e converte unidades conhecidas, incluindo temperatura em °C e pressão ao nível médio do mar em hPa.
3. Interpola grades regulares linearmente e grades irregulares, como a grade ICON global, pelo vizinho mais próximo.
4. Calcula média simples e spread ponto a ponto, utilizando apenas valores finitos.
5. Exige no mínimo dois modelos por célula, ajustável em `config.yml`; células que não atingirem esse requisito ficam ausentes.
6. Calcula um índice de concordância baseado no spread. Ele representa dispersão entre modelos, não probabilidade meteorológica calibrada.
7. Exporta `consensus.nc`, `manifest.json`, `qc.json` e prévias gráficas.

Este método não é correção de viés, pós-processamento estatístico nem previsão probabilística treinada. A média simples é uma referência inicial transparente; pesos por modelo só devem ser introduzidos após validação objetiva contra observações.

## Fontes e atribuição

- [Sigma Meteorologia](https://www.sigmameteorologia.com/): referência funcional pública; este projeto não reutiliza código proprietário.
- [ECMWF Open Data](https://www.ecmwf.int/en/forecasts/datasets/open-data) e [cliente ecmwf-opendata](https://github.com/ecmwf/ecmwf-opendata). Conservar atribuição adequada e respeitar os termos aplicáveis.
- [NOAA/NCEP NOMADS — GFS](https://www.nco.ncep.noaa.gov/pmb/products/gfs/nomads/). Respeitar disponibilidade e diretrizes de requisição.
- [DWD ICON Global GRIB](https://opendata.dwd.de/weather/nwp/icon/grib/). Respeitar termos e atribuição do DWD.

## Validação necessária

Antes de classificar o sistema como operacional, execute uma rodada recente completa nos três provedores. Confirme cobertura, unidades, valores ausentes, intervalos de precipitação e o conteúdo do `manifest.json`, `qc.json` e `consensus.nc`. Não invente substitutos quando uma variável faltar.
