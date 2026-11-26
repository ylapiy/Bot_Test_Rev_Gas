
create or replace view revgas.v_tabelas_completas with (security_invoker = on) as
select tabela, id, dados
from (
  select 1 as ordem, 'clientes' as tabela, c.id,
         (to_jsonb(c) - 'pin_hash' - 'telefone')
           || jsonb_build_object('telefone', revgas.mascarar_telefone(c.telefone),
                                 'tem_pin', c.pin_hash is not null) as dados
    from revgas.clientes c
  union all select 2, 'enderecos',         e.id,  to_jsonb(e)  from revgas.enderecos e
  union all select 3, 'produtos',          p.id,  to_jsonb(p)  from revgas.produtos p
  union all select 4, 'pedidos',           p.id,
         to_jsonb(p) - 'sessao' - 'chave_idempotencia' || jsonb_build_object('sessao', revgas.mascarar_telefone(p.sessao))
    from revgas.pedidos p
  union all select 5, 'pedido_itens',      i.id,  to_jsonb(i)  from revgas.pedido_itens i
  union all select 6, 'mensagens',         m.id,
         to_jsonb(m) - 'sessao' || jsonb_build_object('sessao', revgas.mascarar_telefone(m.sessao))
    from revgas.mensagens m
  union all select 7, 'chamados',          ch.id, to_jsonb(ch) from revgas.chamados ch
  union all select 8, 'eventos_seguranca', ev.id, to_jsonb(ev) from revgas.eventos_seguranca ev
  union all select 9, 'memoria_do_agente', h.id,
         to_jsonb(h) - 'session_id' || jsonb_build_object('session_id', revgas.mascarar_telefone(h.session_id))
    from revgas_memoria.n8n_chat_histories h
) t
order by ordem, id;

revoke all on revgas.v_tabelas_completas from public, anon, authenticated;

select * from revgas.v_tabelas_completas;