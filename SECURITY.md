# Política de segurança

O Prelo processa arquivos enviados por terceiros. Por isso, falhas de segurança são levadas a
sério — obrigado por ajudar a manter o projeto seguro.

## Versões suportadas

Correções de segurança são publicadas na versão mais recente (branch `main`, imagem
`ghcr.io/taigorene/prelo:latest` e a última tag `v*.*.*`). Atualize para ela antes de relatar
um problema.

## Como relatar uma vulnerabilidade

**Não abra uma issue pública.** Use o relato privado do GitHub:
[Report a vulnerability](https://github.com/taigorene/Prelo/security/advisories/new).

Inclua, se possível:

- a versão ou o digest da imagem usada e como o serviço foi iniciado (`docker run`, Compose,
  variáveis `PRELO_*` alteradas);
- um arquivo Markdown (ou `.zip`) mínimo e a requisição que reproduzem o problema;
- o impacto observado (leitura de arquivos, SSRF, negação de serviço, execução de código...).

Você receberá uma resposta assim que possível. Depois da correção, a falha é divulgada em um
*security advisory*, com os devidos créditos, se você quiser.

## Escopo

A seção [Segurança](README.md#segurança) do README descreve as proteções existentes e as
recomendações de implantação. A API não tem autenticação nem limite de requisições por cliente
por design: expor o serviço na internet sem um proxy reverso com autenticação e *rate limiting*
não é considerado uma vulnerabilidade do projeto.
