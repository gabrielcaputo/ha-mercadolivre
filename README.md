# Mercado Livre para Home Assistant

Integração não oficial que acompanha as **compras do Mercado Livre** no Home Assistant:
status de cada pacote, previsão de entrega, último evento do rastreio e, quando o pacote
sai para entrega, a **posição do motorista no mapa**.

> O Mercado Livre não tem API pública para compradores. A integração lê as mesmas páginas
> que o site usa, autenticada com o cookie de sessão da sua conta. Isso pode parar de
> funcionar a qualquer momento se o site mudar. Projeto sem vínculo com o Mercado Livre.

## O que ela cria

**Totais** (somando todas as contas):

| Entidade | Estado | Atributos |
|---|---|---|
| Pacotes a receber | quantidade | `packages`: lista dos pendentes |
| Pacotes a receber hoje | quantidade | `packages` |
| Entregues nos últimos 7 dias | quantidade | `packages`: item, item_info, products, conta, data, imagem, link |
| Link de rastreio compartilhado | (texto) | cole aqui um link de rastreio compartilhado pelo app |

**Por conta:** Sessão (`ok`, `expired`, `blocked`, `error`) e Pacotes a receber.

**Por pacote** (um dispositivo por envio, removido 24 h depois de entregue ou cancelado):

| Entidade | O que mostra |
|---|---|
| Status | `pending`, `handling`, `ready_to_ship`, `shipped`, `out_for_delivery`, `delivered`, `not_delivered`, `cancelled`. Atributos: `item` (1º produto), `item_info` ("+1 produto" quando o pacote tem mais de um), `products` (todos os produtos do pacote: `title`, `image`), textos e passos da página da compra |
| Previsão de entrega | data prometida (atributos com o texto e a janela de horário) |
| Rastreio | último evento do rastreio |
| Localização | último local informado |
| Posição (`device_tracker`) | motorista no mapa enquanto o pacote está saindo para entrega; `home` quando entregue |

## Instalação

### Marketplace / HACS

1. Marketplace (HACS) → ⋮ → **Repositórios personalizados**.
2. URL `https://github.com/gabrielcaputo/ha-mercadolivre`, tipo **Integração**.
3. Instale **Mercado Livre** e reinicie o Home Assistant.

### Manual

Copie o conteúdo deste repositório para `config/custom_components/mercadolivre/` e
reinicie o Home Assistant.

## Configuração

Configurações → Dispositivos e serviços → **Adicionar integração** → Mercado Livre.

### Como pegar o cookie `ssid`

1. Entre em [mercadolivre.com.br](https://www.mercadolivre.com.br) no computador.
2. Abra as ferramentas do desenvolvedor (F12) → **Aplicativo/Armazenamento** → **Cookies** →
   `https://www.mercadolivre.com.br`.
3. Copie o **valor** do cookie `ssid` e cole no campo da integração. Também dá para colar o
   cabeçalho `Cookie` inteiro de uma requisição; a integração tira o `ssid` dele.
4. Opcional: cole o **User-Agent** do mesmo navegador (aparece nas requisições, ou em
   `navigator.userAgent` no console), para a sessão parecer uma só.

Não saia da conta nesse navegador: sair encerra a sessão e invalida o cookie.

### Mais de uma conta

Na página da integração, **Adicionar conta**. Os totais somam todas.

### Sessão expirada

Quando o cookie expira, o sensor **Sessão** vira `expired` e aparece um aviso em
**Reparos**. Entre de novo no site, copie o novo `ssid` e use **⋮ → Reconfigurar** na conta.

## Como ela consulta o site

Para não parecer um robô, as consultas são poucas e irregulares:

- lista de compras a cada ~60 min (com variação de até 15 min); a cada ~10 min quando um
  pacote está saindo para entrega;
- a página de status de um pacote (a mais pesada) só é lida quando a lista mostra mudança,
  ou a cada 3 h;
- pausa entre 0h e 7h, salvo pacote saindo para entrega (desligável nas opções);
- depois de um bloqueio (403/429), para por 3 h, dobrando até 12 h.

A posição do motorista vem do link público de rastreio compartilhado (o mesmo do botão
"compartilhar" do mapa), lido a cada ~30 s, sem cookie. A integração acha o link sozinha na
página de status; só é preciso colá-lo na entidade de texto se ele veio do app.

Endereços são convertidos em coordenadas pelo [Nominatim](https://nominatim.openstreetmap.org)
(OpenStreetMap), com cache.

## Privacidade

- O cookie fica só na configuração do Home Assistant e é enviado apenas ao Mercado Livre.
- O link compartilhado mostra seu endereço a quem o tiver; por isso ele não fica guardado
  como estado da entidade de texto (não vai para o histórico).

## Licença

[MIT](LICENSE).
