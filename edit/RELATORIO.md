# Relatório de Edição — Ep. 01 (App da Barbearia do Samuel)

## 1. Duração: Bruta → Final

| Parte | Bruto (voz) | Mantido no EDL | Retenção |
| --- | ---: | ---: | ---: |
| `p1` | 566,00 s (9m 26s) | 419,07 s (6m 59s) | 74,0% |
| `p2` | 84,42 s (1m 24s) | 11,60 s (0m 12s) | 13,7% |
| `p3` | 567,16 s (9m 27s) | 469,69 s (7m 50s) | 82,8% |
| **Total** | **1.217,58 s (20m 18s)** | **900,36 s (15m 00s)** | **73,95%** |

Meta do brief (`60–75%` da duração bruta) atingida em **73,95%**, com **93 clipes** contínuos na ordem `p1 → p2 → p3` e nenhum intervalo abaixo de `0,08 s`.

## 2. Decisões da revisão e correções aplicadas

### Ordem dos blocos (preços/custos no começo vs. mover para o final)
- **Decisão: mantida a ordem original (`p1 → p2 → p3`).**
- **Por quê:**
  1. Em nenhum momento do material gravado o Antony mostra o app por dentro (`p1 354,74 s`: *"Aqui eu não vou mostrar o app, não vou mostrar o que ele tem"*), então não existe um bloco de demonstração do app para vir antes dos números.
  2. O início do bloco de números (`p1 251–265 s`) é justamente onde ele apresenta o relatório e o nome do app (*"Samuel Barbearia"*), conectado diretamente à frase anterior (*"vou estar mostrando para vocês agora os números"*).
  3. Mover o bloco de preços/custos para depois das ferramentas quebraria referências na fala: em `p1 295,98 s` ele promete no futuro (*"E eu vou te explicar por que que meu custo é tão baixo"*), promessa cumprida logo depois na lista das 10 ferramentas (`p3 22–165 s`); e no bloco de ferramentas/manutenção (`p3 174 s` e `191 s`) ele cita *"com a minha mensalidade de 100 reais"* e *"um pouquinho mais que a mensalidade"*, assumindo que a venda já foi explicada antes.

### Cortes corrigidos (palavra-a-palavra pelo JSON)
- **Eliminado corte no meio de palavra em `p1 235–240 s`:** antes, o clipe 20 começava em `235,24 s` (pegando o final de *"joguinhos"*) e o clipe 21 começava em `240,00 s` (cortando *"O gravador"* no meio). Agora o clipe 19 fecha limpo em `232,18 s` e o clipe 20 segue contínuo de `235,34 s` a `250,44 s` (*"fiz softwares... Como esse próprio gravador, né? O gravador de vídeo..."*).
- **Ajustes finos de borda sem cortar sílabas:** `p1` clipe 11/12 (`146,86 s` / `152,74 s`), `p2` clipe 50 (`8,82–20,42 s`, sem pegar *"é"* nem *"que"*), `p3` clipe 51 (`22,15 s`, margem segura antes de *"quais"*), `p3` Brevo (`128,18 s` / `130,48 s`), `p3` Domínio (`170,40 s` / `173,90 s`), `p3` *"para terminar esse vídeo"* (`447,90 s`, antes cortava o início de *"eu"* em `447,94 s`), e `p3` *"Tchau"* limitado exatamente à duração bruta (`567,16 s`, antes passava para `567,26 s`).

### Decisões do Antony e ajustes visuais
- **Instagram `@aquilles_antony`:** inserido em destaque no cartão final (`overlays.ass` e `plan.json`) e na descrição (`metadata.md`).
- **Frase no gancho (`0:07`):** mantida *"Só que isso quase fez eu perder o cliente, viu?"* conforme decisão do Antony.
- **Cartões e contas (`plan.json` e `overlays.ass`):**
  - Fechada explicitamente a conta do 1º ano no cartão 01: `Lucro 1º ano (2.700 - 160)` → `R$ 2.540`.
  - Alargadas as caixas de `card_receita_ano1`, `card_custos_ia`, `card_quanto_valeria` e dos três cartões de lição (`lic1`, `lic2`, `lic3`) para evitar qualquer toque ou vazamento de texto fora do retângulo.
  - Sincronizados `sec_tempo` (`5:34.80`), `card_tempo` (`5:37.10`) e `R$ 50/hora` (`7:00.51`) com a fala.

### Correções técnicas em `scripts/build.py` e `.github/workflows/render.yml`
- **Trilha opcional corrigida de ponta a ponta:** antes, `ensure_media()` já avisava se `media/trilha.mp3` faltasse, mas `main()` descartava o retorno e `master_audio()` sempre tentava abrir `trilha.mp3` no FFmpeg (causando erro fatal). Agora `main()` passa `trilha_path: Path | None` para `master_audio()`, que segue sem música (apenas voz + SFX) e registra aviso no log e no GitHub Actions.
- **Bug de sintaxe FFmpeg no `zoompan` corrigido:** havia vírgulas escapadas (`\,`) dentro de aspas simples (`z='...'`), o que gera erro de expressão no `av_expr_parse` do FFmpeg. Removidas as barras dentro de aspas simples e adicionado `split` explícito nos chunks.
- **Suporte real a ordem livre de trechos no EDL:** `cut_voice_edl()` e `cut_video_edl()` passaram a usar seek rápido independente (`-ss`/`-t` antes de cada `-i` por clipe), evitando decodificar arquivos inteiros repetidamente e eliminando risco de estourar RAM se trechos forem reordenados fora de ordem cronológica.
- **Preview mais rápido:** `main()` recorta os clipes antes de `ensure_media()`, baixando no `--preview` apenas as partes efetivamente usadas (`p1`).
- **SFX e fontes:** adicionado `asplit` explícito para SFX reutilizados em `master_audio()`; no `render.yml`, priorizadas fontes estáticas da Montserrat (evitando conflito com a versão variável no `libass`) e protegido `fc-list | grep` com `|| true`.

## 3. O que foi cortado do bruto

- **Falso começo inicial (`p1 0:00–33,62 s`):** saudação inicial com pausa longa de 8 s + 14 s de silêncio antes do gancho.
- **Interrupção da ligação e alucinações finais do Whisper (`p1 538,02–566,00 s`).**
- **Aberturas "continuando aqui", ajustes de câmera e tentativa interrompida das ferramentas (`p2 0:00–8,82 s`, `p2 20,42–84,42 s`, `p3 0:00–22,15 s`).**
- **Repetições, falsos começos e pausas > ~0,35 s** ao longo de `p1` e `p3` (fumble mostrando o MacBook, tripla repetição de *"MacBook Air 2014"*, repetição do conselho de PC, repetição de projetos paralelos, falso começo no Brevo, repetição de spam e de *"3 reais por mês"*, falso começo em API Opus, pensamento abandonado em `p3 366–375 s`, *"faço vídeo sem roteiro"*, despedida duplicada e silêncio de 6 s antes de *"Tchau"*).

## 4. Números citados na fala (conferidos)

- **Orçamento de programador (gancho):** pelo menos `R$ 20.000` (`R$ 20 mil`)
- **Gasto resumido para fazer o app:** `~R$ 100`
- **Implantação vendida:** `R$ 1.500` (parcelado em `3x`)
- **Taxa fixa mensal:** `R$ 100/mês`
- **Receita do 1º ano:** `R$ 2.700` (`R$ 1.500` + `12× R$ 100`)
- **Custo do 1º ano (Claude + domínio):** `R$ 160` → **Lucro explícito 1º ano:** `R$ 2.540` (`2.700 - 160`)
- **Receita do 2º ano em diante:** `R$ 1.200/ano` (`R$ 100/mês`)
- **Manutenção anual citada em `p1`:** em torno de `R$ 100/ano`
- **Plano Claude:** `US$ 20/mês` (`~R$ 120`) | **Proporcional ao app:** `R$ 60 a R$ 120`
- **Uso de ChatGPT / Codex:** `4 tarefas pesadas`, `330 mil tokens`, plano de `R$ 99/mês`
- **Tempo no relatório (Claude rodando):** `25 a 35 horas` (média citada em `p2`: `30 horas`)
- **Valor/hora (só implantação ÷ 30 h do Claude):** `R$ 50/hora`
- **Atendimento presencial (Asaas):** terça `4 a 5 horas`, quarta `2 a 3 horas`
- **Serviços grátis (`R$ 0`):** Cloudflare, Supabase (plano grátis), OneSignal, Gmail, Asaas (taxa por cobrança paga pelo cliente), Bluesoft Cosmos (limite diário), GitHub
- **Domínio `.com.br`:** `R$ 40/ano` (`~R$ 3/mês`)
- **Limites / pontos de atenção:** Supabase Pro `US$ 25/mês`; Cloudflare `US$ 5/mês` acima de `100 mil acessos/dia`; Gmail até `500 e-mails/dia` (migrando para Brevo)
- **Estimativa de mercado (programador tradicional):** `80 a 150 horas`, de `R$ 20.000 a R$ 60.000`

## 5. Pontos para conferir ao rodar o `--preview`

1. **Posição da bolinha da câmera vs. cartões:** como não foi possível inspecionar visualmente os frames, os cartões ficaram no topo direito (e topo esquerdo nas lições) e os zooms de tela em `cx=0.46`. Confira no `--preview` (e num trecho de tela do relatório) se nenhum cartão sobrepõe a câmera circular.
2. **Trilha futura:** quando subir `media/trilha.mp3` na release `media`, o `build.py` ativa automaticamente o mix a `-24 dB` com sidechain ducking; enquanto não subir, segue normalmente com aviso.
3. **SFX em `repo/assets/sfx/`:** confira no log do GitHub Actions se os 5 efeitos (`whoosh-short`, `pop`, `click-soft`, `chime`/`ding`, `sparkle`) foram encontrados sem aviso.
