-- =====================================================================
--  RevGás Chatbot – banco de dados (Supabase / PostgreSQL)
--  Projeto de Extensão VI – 2026.2
--
--  Como usar: Supabase → SQL Editor → New query → cole tudo → Run.
--  ANTES de rodar, troque a senha do usuário n8n_bot (procure TROQUE_ESTA_SENHA).
--  O script pode ser rodado de novo sem apagar dados (idempotente).
--  v2: alinhado à especificação OpenAPI da RevGás (https://revgas.stoplight.io),
--      para o banco funcionar como CACHE da API (Customer, Product, Sale).
-- =====================================================================

-- ---------------------------------------------------------------------
-- 0. Extensões e schema
--    O schema "revgas" NÃO é exposto pela API pública do Supabase
--    (só o "public" é), então os dados só são acessíveis pelo banco.
-- ---------------------------------------------------------------------
create extension if not exists pgcrypto with schema extensions;
create schema if not exists revgas;

-- ---------------------------------------------------------------------
-- 1. Usuário do n8n com permissão mínima (princípio do menor privilégio)
--    No n8n, o usuário fica:  n8n_bot.<PROJECT-REF>  (pooler do Supabase)
-- ---------------------------------------------------------------------
do $$
begin
  if not exists (select 1 from pg_roles where rolname = 'n8n_bot') then
    create role n8n_bot login password 'TROQUE_ESTA_SENHA';
  end if;
end $$;
-- A memória do n8n cria a própria tabela ("CREATE TABLE IF NOT EXISTS"), então ela fica
-- num schema separado onde o n8n_bot pode criar tabela, isolado dos dados dos clientes.
create schema if not exists revgas_memoria;
alter role n8n_bot set search_path = revgas_memoria, revgas, extensions;
alter role n8n_bot set statement_timeout = '15s';

-- ---------------------------------------------------------------------
-- 2. Tabelas
-- ---------------------------------------------------------------------
create table if not exists revgas.clientes (
  id                   bigint generated always as identity primary key,
  telefone             text not null unique check (telefone ~ '^[0-9]{10,13}$'),
  nome                 text check (char_length(nome) <= 120),
  pin_hash             text,                       -- só o hash (bcrypt); o PIN nunca é guardado
  pin_tentativas       int  not null default 0,
  pin_bloqueado_ate    timestamptz,
  verificado_ate       timestamptz,                -- sessão verificada por PIN (30 min)
  consentimento_lgpd_em timestamptz,               -- quando aceitou o uso dos dados
  revgas_customer_id   bigint unique,              -- Customer.id na API RevGás (quando houver)
  -- De propósito NÃO guardamos cpf, rg, sexo nem aniversário (existem no Customer da API):
  -- o bot não precisa deles (minimização de dados, LGPD).
  criado_em            timestamptz not null default now(),
  atualizado_em        timestamptz not null default now()
);

create table if not exists revgas.enderecos (
  id           bigint generated always as identity primary key,
  cliente_id   bigint not null references revgas.clientes(id) on delete cascade,
  rua          text not null check (char_length(rua) between 2 and 160),
  numero       text not null check (char_length(numero) between 1 and 20),
  bairro       text not null check (char_length(bairro) between 2 and 80),
  cidade       text not null default 'Teresina' check (char_length(cidade) between 2 and 80),
  uf           char(2) not null default 'PI' check (uf ~ '^[A-Z]{2}$'),
  complemento  text check (char_length(complemento) <= 120),
  referencia   text check (char_length(referencia) <= 160),
  cep          text check (cep ~ '^[0-9]{8}$'),
  principal    boolean not null default true,
  criado_em    timestamptz not null default now()
);
create index if not exists enderecos_cliente_idx on revgas.enderecos (cliente_id);

create table if not exists revgas.produtos (
  id                 bigint generated always as identity primary key,
  codigo             text not null unique,
  nome               text not null,
  api_product_id     bigint unique,                -- Product.id da revenda na API (é o product_id da venda)
  revgas_catalogo_id bigint,                       -- Product.revgas_product_id (catálogo global RevGás)
  api_container_id   bigint,                       -- Product.container_id (vasilhame associado)
  preco              numeric(10,2) not null check (preco > 0),  -- a API não tem preço: fica aqui
  ativo              boolean not null default true,             -- inverso de Product.inactive
  estoque            int,                                       -- cache de Product.stock_quantity
  estoque_atualizado_em timestamptz,
  atualizado_em      timestamptz not null default now()
);

create table if not exists revgas.pedidos (
  id                bigint generated always as identity primary key,
  cliente_id        bigint not null references revgas.clientes(id),
  endereco_id       bigint not null references revgas.enderecos(id),
  status            text not null default 'recebido'           -- etapa do lado do bot
                    check (status in ('recebido','enviado_revgas','erro_api','cancelado')),
  status_revgas     text check (status_revgas in ('CANCEL','OPEN','ACTIVE','WARNING','CONFIRM','CHECK')),  -- Sale.status
  canal             text not null default 'WHATSAPP' check (canal in ('DISK','SINO','WHATSAPP')),          -- Sale.channel
  entregador_nome   text,                                       -- Sale.deliverer_name
  entregue_em       timestamptz,                                -- Sale.delivered_at
  sincronizado_em   timestamptz,                                -- última leitura de GET /sales/{id}
  forma_pagamento   text not null
                    check (forma_pagamento in ('PIX','MONEY','CARTAO_DE_DEBITO','CARTAO_DE_CREDITO')),
  troco_para        numeric(10,2) check (troco_para is null or troco_para > 0),
  valor_total       numeric(10,2) not null check (valor_total > 0),
  revgas_venda_id   bigint,
  erro_api          text,
  observacoes       text check (char_length(observacoes) <= 255),
  sessao            text,
  chave_idempotencia text unique,                  -- impede pedido duplicado
  criado_em         timestamptz not null default now(),
  atualizado_em     timestamptz not null default now()
);
create index if not exists pedidos_cliente_idx on revgas.pedidos (cliente_id, criado_em desc);

create table if not exists revgas.pedido_itens (
  id              bigint generated always as identity primary key,
  pedido_id       bigint not null references revgas.pedidos(id) on delete cascade,
  produto_id      bigint not null references revgas.produtos(id),
  quantidade      int not null check (quantidade between 1 and 10),   -- SaleItem.quantity
  preco_unitario  numeric(10,2) not null check (preco_unitario > 0),  -- SaleItem.value
  desconto        numeric(10,2) not null default 0 check (desconto >= 0)  -- SaleItem.discount
);

-- Histórico das conversas (para análise de intenções). Guardado por 90 dias.
create table if not exists revgas.mensagens (
  id          bigint generated always as identity primary key,
  cliente_id  bigint references revgas.clientes(id) on delete set null,
  sessao      text not null,
  direcao     text not null check (direcao in ('cliente','bot')),
  texto       text not null check (char_length(texto) <= 4000),
  intencao    text,
  criado_em   timestamptz not null default now()
);
create index if not exists mensagens_data_idx on revgas.mensagens (criado_em);

create table if not exists revgas.chamados (
  id          bigint generated always as identity primary key,
  cliente_id  bigint references revgas.clientes(id) on delete set null,
  motivo      text not null check (motivo in ('pediu_atendente','reclamacao','emergencia','pin_bloqueado','erro_sistema','outro')),
  mensagem    text check (char_length(mensagem) <= 1000),
  status      text not null default 'aberto' check (status in ('aberto','em_atendimento','resolvido')),
  criado_em   timestamptz not null default now()
);

-- Auditoria de segurança (tentativas de PIN, acessos negados etc.)
create table if not exists revgas.eventos_seguranca (
  id          bigint generated always as identity primary key,
  cliente_id  bigint references revgas.clientes(id) on delete set null,
  tipo        text not null,
  detalhe     text,
  criado_em   timestamptz not null default now()
);

-- Memória do agente (formato usado pelo nó "Postgres Chat Memory" do n8n)
create table if not exists revgas_memoria.n8n_chat_histories (
  id          serial primary key,
  session_id  varchar(255) not null,
  message     jsonb not null,
  criado_em   timestamptz not null default now()
);
create index if not exists chat_hist_sessao_idx on revgas_memoria.n8n_chat_histories (session_id);

-- ---------------------------------------------------------------------
-- 2b. Migração da v1 (só faz algo se a versão anterior já foi rodada)
-- ---------------------------------------------------------------------
do $$
begin
  if exists (select 1 from information_schema.columns where table_schema = 'revgas'
             and table_name = 'produtos' and column_name = 'revgas_product_id')
     and not exists (select 1 from information_schema.columns where table_schema = 'revgas'
             and table_name = 'produtos' and column_name = 'api_product_id') then
    alter table revgas.produtos rename column revgas_product_id to api_product_id;
    alter table revgas.produtos alter column api_product_id type bigint;
  end if;
end $$;
alter table revgas.clientes  add column if not exists revgas_customer_id bigint unique;
alter table revgas.enderecos add column if not exists cep text check (cep ~ '^[0-9]{8}$');
alter table revgas.produtos  add column if not exists revgas_catalogo_id bigint;
alter table revgas.produtos  add column if not exists api_container_id bigint;
alter table revgas.produtos  add column if not exists estoque int;
alter table revgas.produtos  add column if not exists estoque_atualizado_em timestamptz;
alter table revgas.pedidos   add column if not exists status_revgas text
  check (status_revgas in ('CANCEL','OPEN','ACTIVE','WARNING','CONFIRM','CHECK'));
alter table revgas.pedidos   add column if not exists canal text not null default 'WHATSAPP'
  check (canal in ('DISK','SINO','WHATSAPP'));
alter table revgas.pedidos   add column if not exists entregador_nome text;
alter table revgas.pedidos   add column if not exists entregue_em timestamptz;
alter table revgas.pedidos   add column if not exists sincronizado_em timestamptz;
alter table revgas.pedido_itens add column if not exists desconto numeric(10,2) not null default 0;
do $$
begin
  update revgas.pedidos set status = 'recebido' where status in ('saiu_para_entrega','entregue');
  alter table revgas.pedidos drop constraint if exists pedidos_status_check;
  alter table revgas.pedidos add constraint pedidos_status_check
    check (status in ('recebido','enviado_revgas','erro_api','cancelado'));
end $$;

-- ---------------------------------------------------------------------
-- 3. Atualiza "atualizado_em" automaticamente
-- ---------------------------------------------------------------------
create or replace function revgas.tg_atualizado_em() returns trigger
language plpgsql as $$
begin new.atualizado_em := now(); return new; end $$;

drop trigger if exists clientes_atualizado on revgas.clientes;
create trigger clientes_atualizado before update on revgas.clientes
  for each row execute function revgas.tg_atualizado_em();
drop trigger if exists pedidos_atualizado on revgas.pedidos;
create trigger pedidos_atualizado before update on revgas.pedidos
  for each row execute function revgas.tg_atualizado_em();
drop trigger if exists produtos_atualizado on revgas.produtos;
create trigger produtos_atualizado before update on revgas.produtos
  for each row execute function revgas.tg_atualizado_em();

-- ---------------------------------------------------------------------
-- 4. Funções auxiliares
-- ---------------------------------------------------------------------
-- Telefone mascarado para mostrar ao cliente: 86912345678 -> (86) 9****-5678
create or replace function revgas.mascarar_telefone(t text) returns text
language sql immutable as $$
  select case when n ~ '^[0-9]{10,11}$' then
    '(' || left(n, 2) || ') ' || substr(n, 3, 1) || '****-' || right(n, 4)
  else '***' end
  from (select regexp_replace(regexp_replace(coalesce(t, ''), '\D', '', 'g'), '^55(?=[0-9]{10,11}$)', '') as n) x
$$;

-- Normaliza telefone: só dígitos, sem o 55 do Brasil
create or replace function revgas.normalizar_telefone(t text) returns text
language sql immutable as $$
  select regexp_replace(regexp_replace(coalesce(t, ''), '\D', '', 'g'), '^55(?=[0-9]{10,11}$)', '')
$$;

-- Lê valor em reais escrito de qualquer jeito: "R$ 250,00", "250", "1.250,50"
create or replace function revgas.ler_valor(t text) returns numeric
language plpgsql immutable as $$
declare s text := regexp_replace(coalesce(t, ''), '[^0-9.,]', '', 'g');
begin
  if s = '' then return null; end if;
  if s like '%,%' then s := replace(replace(s, '.', ''), ',', '.'); end if;
  return round(s::numeric, 2);
exception when others then return null;
end $$;

-- Cria o cliente se ainda não existir e devolve o id
create or replace function revgas.garantir_cliente(p_telefone text) returns bigint
language plpgsql security definer set search_path = revgas, extensions as $$
declare v_tel text := revgas.normalizar_telefone(p_telefone); v_id bigint;
begin
  if v_tel !~ '^[0-9]{10,11}$' then raise exception 'telefone inválido'; end if;
  insert into revgas.clientes (telefone) values (v_tel)
  on conflict (telefone) do nothing;
  select id into v_id from revgas.clientes where telefone = v_tel;
  return v_id;
end $$;

-- ---------------------------------------------------------------------
-- 5. Confirmação de identidade por PIN
--    - PIN de 4 dígitos, guardado só como hash bcrypt
--    - 3 erros seguidos = bloqueio de 15 minutos + chamado para atendente
--    - PIN correto = sessão verificada por 30 minutos
-- ---------------------------------------------------------------------
create or replace function revgas.criar_pin(p_telefone text, p_pin text) returns jsonb
language plpgsql security definer set search_path = revgas, extensions as $$
declare v_id bigint; v_hash text;
begin
  v_id := revgas.garantir_cliente(p_telefone);
  select pin_hash into v_hash from revgas.clientes where id = v_id;
  if v_hash is not null then
    return jsonb_build_object('resultado', 'ja_tem_pin');
  end if;
  if p_pin !~ '^[0-9]{4}$' then
    return jsonb_build_object('resultado', 'formato_invalido', 'regra', 'O PIN deve ter exatamente 4 números.');
  end if;
  if p_pin ~ '^(\d)\1{3}$' or p_pin in ('1234','4321','0123','9876','1212','2580')
     or p_pin = right(revgas.normalizar_telefone(p_telefone), 4) then
    return jsonb_build_object('resultado', 'pin_fraco', 'regra', 'Evite números repetidos, sequências ou o final do telefone.');
  end if;
  update revgas.clientes
     set pin_hash = crypt(p_pin, gen_salt('bf', 8)), pin_tentativas = 0,
         pin_bloqueado_ate = null, verificado_ate = now() + interval '30 minutes'
   where id = v_id;
  insert into revgas.eventos_seguranca (cliente_id, tipo) values (v_id, 'pin_criado');
  return jsonb_build_object('resultado', 'ok');
end $$;

create or replace function revgas.verificar_pin(p_telefone text, p_pin text) returns jsonb
language plpgsql security definer set search_path = revgas, extensions as $$
declare c revgas.clientes%rowtype;
begin
  select * into c from revgas.clientes where telefone = revgas.normalizar_telefone(p_telefone);
  if not found or c.pin_hash is null then
    return jsonb_build_object('resultado', 'sem_pin');
  end if;
  if c.pin_bloqueado_ate is not null and c.pin_bloqueado_ate > now() then
    insert into revgas.eventos_seguranca (cliente_id, tipo) values (c.id, 'pin_tentativa_bloqueado');
    return jsonb_build_object('resultado', 'bloqueado',
      'minutos_restantes', ceil(extract(epoch from (c.pin_bloqueado_ate - now())) / 60));
  end if;
  if coalesce(p_pin, '') ~ '^[0-9]{4}$' and crypt(p_pin, c.pin_hash) = c.pin_hash then
    update revgas.clientes set pin_tentativas = 0, pin_bloqueado_ate = null,
           verificado_ate = now() + interval '30 minutes' where id = c.id;
    insert into revgas.eventos_seguranca (cliente_id, tipo) values (c.id, 'pin_ok');
    return jsonb_build_object('resultado', 'ok');
  end if;
  if c.pin_tentativas + 1 >= 3 then
    update revgas.clientes set pin_tentativas = 0, verificado_ate = null,
           pin_bloqueado_ate = now() + interval '15 minutes' where id = c.id;
    insert into revgas.eventos_seguranca (cliente_id, tipo) values (c.id, 'pin_bloqueado');
    insert into revgas.chamados (cliente_id, motivo, mensagem)
      values (c.id, 'pin_bloqueado', '3 tentativas de PIN erradas. Confirmar identidade por outro meio.');
    return jsonb_build_object('resultado', 'bloqueado', 'minutos_restantes', 15);
  end if;
  update revgas.clientes set pin_tentativas = pin_tentativas + 1 where id = c.id;
  insert into revgas.eventos_seguranca (cliente_id, tipo) values (c.id, 'pin_erro');
  return jsonb_build_object('resultado', 'incorreto', 'tentativas_restantes', 2 - c.pin_tentativas);
end $$;

create or replace function revgas.esta_verificado(p_telefone text) returns boolean
language sql stable security definer set search_path = revgas, extensions as $$
  select coalesce((select verificado_ate > now() from revgas.clientes
                    where telefone = revgas.normalizar_telefone(p_telefone)), false)
$$;

-- Encerra a verificação (ex.: cliente disse "sair" ou trocou de assunto sensível)
create or replace function revgas.encerrar_verificacao(p_telefone text) returns void
language sql security definer set search_path = revgas, extensions as $$
  update revgas.clientes set verificado_ate = null where telefone = revgas.normalizar_telefone(p_telefone)
$$;

-- ---------------------------------------------------------------------
-- 6. Consultas usadas pelo bot
--    Dados pessoais (nome completo, endereço, pedidos) só saem
--    se o cliente estiver verificado por PIN. A regra fica NO BANCO,
--    não depende da IA obedecer ao prompt.
-- ---------------------------------------------------------------------
create or replace function revgas.consultar_cliente(p_telefone text) returns jsonb
language plpgsql stable security definer set search_path = revgas, extensions as $$
declare c revgas.clientes%rowtype; e revgas.enderecos%rowtype; v_ok boolean;
begin
  select * into c from revgas.clientes where telefone = revgas.normalizar_telefone(p_telefone);
  if not found then
    return jsonb_build_object('cadastrado', false, 'tem_pin', false, 'verificado', false);
  end if;
  v_ok := c.verificado_ate is not null and c.verificado_ate > now();
  if not v_ok then
    return jsonb_build_object('cadastrado', c.nome is not null, 'tem_pin', c.pin_hash is not null,
      'verificado', false, 'bloqueado', coalesce(c.pin_bloqueado_ate > now(), false),
      'aviso', 'Peça o PIN antes de mostrar nome, endereço ou pedidos.');
  end if;
  select * into e from revgas.enderecos where cliente_id = c.id and principal
   order by criado_em desc limit 1;
  return jsonb_build_object('cadastrado', c.nome is not null, 'tem_pin', true, 'verificado', true,
    'nome', c.nome, 'telefone_mascarado', revgas.mascarar_telefone(c.telefone),
    'ultimo_endereco', case when e.id is null then null else jsonb_build_object(
      'rua', e.rua, 'numero', e.numero, 'bairro', e.bairro, 'cidade', e.cidade, 'uf', e.uf,
      'complemento', e.complemento, 'referencia', e.referencia) end);
end $$;

create or replace function revgas.consultar_pedidos(p_telefone text) returns jsonb
language plpgsql stable security definer set search_path = revgas, extensions as $$
declare v_id bigint; v_ok boolean;
begin
  select id, coalesce(verificado_ate > now(), false) into v_id, v_ok
    from revgas.clientes where telefone = revgas.normalizar_telefone(p_telefone);
  if v_id is null then return jsonb_build_object('verificado', false, 'pedidos', '[]'::jsonb); end if;
  if not v_ok then
    return jsonb_build_object('verificado', false, 'aviso', 'Peça o PIN antes de mostrar os pedidos.');
  end if;
  return jsonb_build_object('verificado', true, 'pedidos', coalesce((
    select jsonb_agg(x order by x->>'data' desc) from (
      select jsonb_build_object('numero', p.id, 'data', to_char(p.criado_em at time zone 'America/Fortaleza', 'DD/MM/YYYY HH24:MI'),
        'status', case p.status_revgas
                    when 'OPEN' then 'aberto' when 'ACTIVE' then 'em andamento'
                    when 'WARNING' then 'entregador avisado' when 'CONFIRM' then 'entregue'
                    when 'CHECK' then 'entregue e conferido' when 'CANCEL' then 'cancelado'
                    else case p.status when 'erro_api' then 'aguardando confirmação de um atendente'
                                       when 'cancelado' then 'cancelado' else 'recebido' end end,
        'entregador', p.entregador_nome,
        'valor_total', p.valor_total, 'revgas_venda_id', p.revgas_venda_id,
        'itens', (select string_agg(i.quantidade || 'x ' || pr.nome, ', ')
                    from revgas.pedido_itens i join revgas.produtos pr on pr.id = i.produto_id
                   where i.pedido_id = p.id)) as x
        from revgas.pedidos p where p.cliente_id = v_id
       order by p.criado_em desc limit 5) t), '[]'::jsonb));
end $$;

create or replace function revgas.catalogo() returns jsonb
language sql stable security definer set search_path = revgas, extensions as $$
  select coalesce(jsonb_agg(jsonb_build_object('codigo', codigo, 'nome', nome, 'preco', preco,
         'api_product_id', api_product_id,
         'estoque', case when estoque is null then 'desconhecido'
                         when estoque <= 0 then 'em falta' else estoque::text end) order by id), '[]'::jsonb)
    from revgas.produtos where ativo
$$;

-- ---------------------------------------------------------------------
-- 7. Registrar pedido
--    - O PREÇO vem do banco, nunca da IA (evita preço inventado ou desconto).
--    - Chave de idempotência evita pedido duplicado se a mensagem repetir.
--    - Retorna tudo que o n8n precisa para mandar a venda à API RevGás.
-- ---------------------------------------------------------------------
create or replace function revgas.registrar_pedido(p jsonb) returns jsonb
language plpgsql security definer set search_path = revgas, extensions as $$
declare
  v_cli bigint; v_end bigint; v_ped bigint; v_prod revgas.produtos%rowtype;
  v_qtd int; v_total numeric(10,2); v_pag text; v_troco numeric(10,2); v_exist jsonb;
begin
  if p->>'chave_idempotencia' is not null then
    select jsonb_build_object('pedido_id', pe.id, 'duplicado', true, 'valor_total', pe.valor_total,
             'pagamento', pe.forma_pagamento, 'troco_para', pe.troco_para,
             'produto', pr.nome, 'api_product_id', pr.api_product_id,
             'quantidade', i.quantidade, 'preco_unitario', i.preco_unitario)
      into v_exist
      from revgas.pedidos pe
      join revgas.pedido_itens i on i.pedido_id = pe.id
      join revgas.produtos pr on pr.id = i.produto_id
     where pe.chave_idempotencia = p->>'chave_idempotencia'
     limit 1;
    if v_exist is not null then return v_exist; end if;
  end if;

  select * into v_prod from revgas.produtos
   where ativo and (codigo = upper(p->>'produto_codigo')
                    or api_product_id::text = coalesce(p->>'api_product_id', p->>'product_id'))
   limit 1;
  if not found then return jsonb_build_object('erro', 'produto_invalido'); end if;
  if v_prod.estoque is not null and v_prod.estoque <= 0 then
    return jsonb_build_object('erro', 'sem_estoque', 'produto', v_prod.nome);
  end if;

  v_qtd := coalesce(nullif(p->>'quantidade', '')::int, 1);
  if v_qtd < 1 or v_qtd > 10 then return jsonb_build_object('erro', 'quantidade_invalida'); end if;

  v_pag := upper(coalesce(p->>'pagamento', ''));
  if v_pag not in ('PIX','MONEY','CARTAO_DE_DEBITO','CARTAO_DE_CREDITO') then
    return jsonb_build_object('erro', 'pagamento_invalido');
  end if;
  v_total := v_prod.preco * v_qtd;
  v_troco := revgas.ler_valor(p->>'troco');
  if v_troco is not null and v_troco < v_total then v_troco := null; end if;

  if char_length(trim(coalesce(p->>'rua', ''))) < 2 or trim(coalesce(p->>'numero', '')) = ''
     or char_length(trim(coalesce(p->>'bairro', ''))) < 2 then
    return jsonb_build_object('erro', 'endereco_incompleto');
  end if;

  v_cli := revgas.garantir_cliente(p->>'telefone');
  update revgas.clientes set nome = coalesce(nullif(left(p->>'nome', 120), ''), nome),
         consentimento_lgpd_em = coalesce(consentimento_lgpd_em, now())
   where id = v_cli;

  update revgas.enderecos set principal = false where cliente_id = v_cli;
  insert into revgas.enderecos (cliente_id, rua, numero, bairro, cidade, uf, complemento, referencia, cep, principal)
  values (v_cli, left(p->>'rua', 160), left(p->>'numero', 20), left(p->>'bairro', 80),
          coalesce(nullif(left(p->>'cidade', 80), ''), 'Teresina'),
          upper(coalesce(nullif(left(p->>'uf', 2), ''), 'PI')),
          nullif(left(p->>'complemento', 120), ''), nullif(left(p->>'referencia', 160), ''),
          nullif(regexp_replace(coalesce(p->>'cep', ''), '\D', '', 'g'), ''), true)
  returning id into v_end;

  insert into revgas.pedidos (cliente_id, endereco_id, forma_pagamento, troco_para, valor_total,
                              observacoes, sessao, chave_idempotencia)
  values (v_cli, v_end, v_pag, v_troco, v_total, nullif(left(p->>'observacoes', 255), ''),
          p->>'sessao', p->>'chave_idempotencia')
  returning id into v_ped;
  insert into revgas.pedido_itens (pedido_id, produto_id, quantidade, preco_unitario)
  values (v_ped, v_prod.id, v_qtd, v_prod.preco);

  return jsonb_build_object('pedido_id', v_ped, 'duplicado', false,
    'produto', v_prod.nome, 'api_product_id', v_prod.api_product_id,
    'quantidade', v_qtd, 'preco_unitario', v_prod.preco, 'valor_total', v_total,
    'pagamento', v_pag, 'troco_para', v_troco);
exception when check_violation or invalid_text_representation or numeric_value_out_of_range
               or string_data_right_truncation or raise_exception then
  return jsonb_build_object('erro', 'dados_invalidos', 'detalhe', sqlerrm);
end $$;

-- Grava o resultado do POST /sales no pedido
create or replace function revgas.atualizar_envio_revgas(p_pedido_id bigint, p_venda_id bigint, p_erro text)
returns void language sql security definer set search_path = revgas, extensions as $$
  update revgas.pedidos
     set revgas_venda_id = p_venda_id,
         status = case when p_venda_id is not null then 'enviado_revgas' else 'erro_api' end,
         status_revgas = case when p_venda_id is not null then coalesce(status_revgas, 'ACTIVE') else status_revgas end,
         erro_api = left(p_erro, 500)
   where id = p_pedido_id
$$;

-- Monta o corpo EXATO do POST /sales (schema da API) a partir do pedido gravado.
-- Assim o formato fica num lugar só e o n8n só repassa.
create or replace function revgas.corpo_venda_api(p_pedido_id bigint, p_customer_id bigint, p_establishment_id bigint)
returns jsonb language sql stable security definer set search_path = revgas, extensions as $$
  select jsonb_strip_nulls(jsonb_build_object(
    'customer_id', coalesce(c.revgas_customer_id, p_customer_id),
    'establishment_id', p_establishment_id,
    'location', e.rua, 'number', e.numero, 'neighborhood', e.bairro,
    'city', e.cidade, 'state_federation', e.uf,
    'complement', nullif(e.complemento, ''), 'landmark', nullif(e.referencia, ''),
    'observation', left(concat_ws(' ',
        'Pedido via chatbot RevGás (Projeto de Extensão). Pedido nº ' || p.id || '.',
        case when p.forma_pagamento = 'MONEY' and p.troco_para is not null
             then 'Troco para R$ ' || replace(to_char(p.troco_para, 'FM999990.00'), '.', ',') || '.' end,
        p.observacoes), 255),
    'products', (select jsonb_agg(jsonb_build_object('product_id', pr.api_product_id,
                   'quantity', i.quantidade, 'value', i.preco_unitario))
                   from revgas.pedido_itens i join revgas.produtos pr on pr.id = i.produto_id
                  where i.pedido_id = p.id),
    'payments', jsonb_build_array(jsonb_build_object('payment_type', p.forma_pagamento, 'value', p.valor_total)),
    'channel', p.canal, 'origin', 'INTEGRATION', 'option', 'NORMAL', 'broadcast', 'NONE'))
  from revgas.pedidos p
  join revgas.clientes c on c.id = p.cliente_id
  join revgas.enderecos e on e.id = p.endereco_id
  where p.id = p_pedido_id
$$;

-- Atualiza o cache de estoque com a resposta de GET /products (o array "data")
create or replace function revgas.sincronizar_produtos(p_dados jsonb) returns int
language plpgsql security definer set search_path = revgas, extensions as $$
declare n int;
begin
  update revgas.produtos pr
     set estoque = case when (x->>'inactive')::boolean then 0 else (x->>'stock_quantity')::int end,
         revgas_catalogo_id = coalesce(nullif(x->>'revgas_product_id', '')::bigint, pr.revgas_catalogo_id),
         api_container_id = coalesce(nullif(x->>'container_id', '')::bigint, pr.api_container_id),
         estoque_atualizado_em = now()
    from jsonb_array_elements(coalesce(p_dados, '[]'::jsonb)) x
   where pr.api_product_id = (x->>'id')::bigint;
  get diagnostics n = row_count;
  return n;
end $$;

-- Atualiza o cache de uma venda com a resposta de GET /sales/{id}
create or replace function revgas.sincronizar_venda(p_venda jsonb) returns jsonb
language plpgsql security definer set search_path = revgas, extensions as $$
declare v_id bigint;
begin
  update revgas.pedidos
     set status_revgas = case when p_venda->>'status' in ('CANCEL','OPEN','ACTIVE','WARNING','CONFIRM','CHECK')
                              then p_venda->>'status' else status_revgas end,
         status = case when p_venda->>'status' = 'CANCEL' then 'cancelado' else status end,
         entregador_nome = coalesce(p_venda->>'deliverer_name', entregador_nome),
         entregue_em = coalesce(nullif(p_venda->>'delivered_at', '')::timestamptz, entregue_em),
         sincronizado_em = now()
   where revgas_venda_id = (p_venda->>'id')::bigint
  returning id into v_id;
  return jsonb_build_object('pedido_id', v_id, 'status_revgas', p_venda->>'status');
end $$;

-- Vendas em andamento, para o n8n sincronizar o status pela API (sem dados pessoais)
create or replace function revgas.vendas_em_aberto() returns table (revgas_venda_id bigint)
language sql stable security definer set search_path = revgas, extensions as $$
  select p.revgas_venda_id
    from revgas.pedidos p
   where p.revgas_venda_id is not null
     and coalesce(p.status_revgas, 'ACTIVE') in ('OPEN', 'ACTIVE', 'WARNING')
     and p.criado_em > now() - interval '2 days'
   order by p.criado_em desc
   limit 20
$$;

-- Registra mensagem (cliente ou bot) para análise; cria chamado se for o caso
create or replace function revgas.registrar_mensagem(p_telefone text, p_sessao text, p_direcao text,
                                                    p_texto text, p_intencao text default null)
returns void language plpgsql security definer set search_path = revgas, extensions as $$
declare v_cli bigint;
begin
  select id into v_cli from revgas.clientes where telefone = revgas.normalizar_telefone(p_telefone);
  insert into revgas.mensagens (cliente_id, sessao, direcao, texto, intencao)
  values (v_cli, coalesce(p_sessao, 'sem_sessao'), p_direcao, left(coalesce(p_texto, ''), 4000), p_intencao);
end $$;

create or replace function revgas.abrir_chamado(p_telefone text, p_motivo text, p_mensagem text)
returns bigint language plpgsql security definer set search_path = revgas, extensions as $$
declare v_id bigint;
begin
  insert into revgas.chamados (cliente_id, motivo, mensagem)
  values ((select id from revgas.clientes where telefone = revgas.normalizar_telefone(p_telefone)),
          case when p_motivo in ('pediu_atendente','reclamacao','emergencia','pin_bloqueado','erro_sistema')
               then p_motivo else 'outro' end,
          left(p_mensagem, 1000))
  returning id into v_id;
  return v_id;
end $$;

-- ---------------------------------------------------------------------
-- 8. LGPD: apagar dados antigos e atender pedido de exclusão
-- ---------------------------------------------------------------------
create or replace function revgas.limpar_dados_antigos() returns jsonb
language plpgsql security definer set search_path = revgas, extensions as $$
declare a int; b int; c int;
begin
  delete from revgas.mensagens where criado_em < now() - interval '90 days';
  get diagnostics a = row_count;
  delete from revgas_memoria.n8n_chat_histories where criado_em < now() - interval '7 days';
  get diagnostics b = row_count;
  delete from revgas.eventos_seguranca where criado_em < now() - interval '180 days';
  get diagnostics c = row_count;
  return jsonb_build_object('mensagens', a, 'memoria', b, 'eventos', c);
end $$;

-- Anonimiza um cliente a pedido dele (direito de exclusão da LGPD).
-- Pedidos ficam (obrigação fiscal), mas sem nome, telefone e endereço.
create or replace function revgas.anonimizar_cliente(p_telefone text) returns boolean
language plpgsql security definer set search_path = revgas, extensions as $$
declare v_id bigint;
begin
  select id into v_id from revgas.clientes where telefone = revgas.normalizar_telefone(p_telefone);
  if v_id is null then return false; end if;
  update revgas.enderecos set rua = 'removido', numero = '0', bairro = 'removido',
         complemento = null, referencia = null where cliente_id = v_id;
  update revgas.clientes set telefone = '000' || lpad(v_id::text, 10, '0'), nome = null, pin_hash = null,
         verificado_ate = null where id = v_id;
  delete from revgas.mensagens where cliente_id = v_id;
  delete from revgas_memoria.n8n_chat_histories where session_id = revgas.normalizar_telefone(p_telefone);
  insert into revgas.eventos_seguranca (cliente_id, tipo) values (v_id, 'cliente_anonimizado');
  return true;
end $$;

-- ---------------------------------------------------------------------
-- 9. Views para a análise das conversas (usadas no painel do Supabase)
-- ---------------------------------------------------------------------
create or replace view revgas.v_intencoes as
  select intencao, count(*) as mensagens,
         round(100.0 * count(*) / nullif(sum(count(*)) over (), 0), 1) as percentual
    from revgas.mensagens where direcao = 'cliente' and intencao is not null
   group by intencao order by mensagens desc;

create or replace view revgas.v_pedidos_por_dia as
  select (criado_em at time zone 'America/Fortaleza')::date as dia, count(*) as pedidos,
         sum(valor_total) as faturamento,
         count(*) filter (where status = 'erro_api') as com_erro_api
    from revgas.pedidos group by 1 order by 1 desc;

create or replace view revgas.v_seguranca as
  select tipo, count(*) as total, max(criado_em) as ultimo
    from revgas.eventos_seguranca group by tipo order by total desc;

-- ---------------------------------------------------------------------
-- 10. Segurança de acesso
--     - RLS ligado em todas as tabelas
--     - anon/authenticated (API pública) sem acesso nenhum
--     - n8n_bot lê o catálogo, usa a memória e chama as funções.
--       NÃO lê a tabela de clientes direto (nem o hash do PIN).
-- ---------------------------------------------------------------------
do $$
declare t text;
begin
  foreach t in array array['clientes','enderecos','produtos','pedidos','pedido_itens','mensagens',
                           'chamados','eventos_seguranca'] loop
    execute format('alter table revgas.%I enable row level security', t);
  end loop;
  alter table revgas_memoria.n8n_chat_histories enable row level security;
end $$;

revoke all on schema revgas, revgas_memoria from public, anon, authenticated;
revoke all on all tables in schema revgas_memoria from public, anon, authenticated;
revoke all on all tables    in schema revgas from public, anon, authenticated;
revoke all on all functions in schema revgas from public, anon, authenticated;

grant usage on schema revgas to n8n_bot;
grant usage on schema extensions to n8n_bot;

-- catálogo: só leitura
grant select on revgas.produtos to n8n_bot;
drop policy if exists bot_le_produtos on revgas.produtos;
create policy bot_le_produtos on revgas.produtos for select to n8n_bot using (ativo);

-- memória do agente: o nó do n8n precisa ler e gravar
grant usage, create on schema revgas_memoria to n8n_bot;
grant select, insert, delete on revgas_memoria.n8n_chat_histories to n8n_bot;
grant usage, select on sequence revgas_memoria.n8n_chat_histories_id_seq to n8n_bot;
drop policy if exists bot_memoria on revgas_memoria.n8n_chat_histories;
create policy bot_memoria on revgas_memoria.n8n_chat_histories for all to n8n_bot using (true) with check (true);

-- funções que o bot pode chamar
grant execute on function
  revgas.mascarar_telefone(text), revgas.normalizar_telefone(text), revgas.ler_valor(text),
  revgas.criar_pin(text, text), revgas.verificar_pin(text, text),
  revgas.esta_verificado(text), revgas.encerrar_verificacao(text),
  revgas.consultar_cliente(text), revgas.consultar_pedidos(text), revgas.catalogo(),
  revgas.registrar_pedido(jsonb), revgas.atualizar_envio_revgas(bigint, bigint, text),
  revgas.corpo_venda_api(bigint, bigint, bigint), revgas.sincronizar_produtos(jsonb),
  revgas.sincronizar_venda(jsonb), revgas.vendas_em_aberto(),
  revgas.registrar_mensagem(text, text, text, text, text), revgas.abrir_chamado(text, text, text)
to n8n_bot;
-- garantir_cliente, limpar_dados_antigos e anonimizar_cliente ficam só para o administrador.

-- ---------------------------------------------------------------------
-- 11. Produtos (preços de EXEMPLO – ajuste com a RevGás).
--     api_product_id = Product.id em GET /products da revenda 2580.
-- ---------------------------------------------------------------------
insert into revgas.produtos (codigo, nome, api_product_id, preco) values
  ('P13',    'Botijão P13 (13 kg)', 26717, 110.00),
  ('P45',    'Botijão P45 (45 kg)', 23974, 420.00),
  ('AGUA20', 'Água mineral 20 L',   26255,  15.00),
  ('VAS13',  'Vasilhame P13 vazio', 26718, 180.00)
on conflict (codigo) do nothing;

-- ---------------------------------------------------------------------
-- 12. (Opcional) Limpeza automática diária às 3h (requer a extensão pg_cron:
--     Database → Extensions → pg_cron → Enable). Depois rode:
--   select cron.schedule('revgas-limpeza', '0 6 * * *', 'select revgas.limpar_dados_antigos()');
-- ---------------------------------------------------------------------
