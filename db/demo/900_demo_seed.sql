-- Fictional deterministic fixtures for the credential-free portfolio demo.
INSERT INTO macro_series (series_id, observed_at, value, source) VALUES
('T10Y2Y', NOW() - INTERVAL '1 hour', 0.42, 'demo'),
('VIXCLS', NOW() - INTERVAL '1 hour', 15.8, 'demo'),
('DTWEXBGS', NOW() - INTERVAL '1 hour', 121.4, 'demo'),
('BAMLH0A0HYM2', NOW() - INTERVAL '1 hour', 3.21, 'demo'),
('DGS10', NOW() - INTERVAL '1 hour', 4.18, 'demo'),
('T5YIE', NOW() - INTERVAL '1 hour', 2.31, 'demo');

INSERT INTO econ_events (event_id, event_name, country, scheduled_at, impact_level, consensus, previous, source, metadata) VALUES
('demo-us-cpi', 'US CPI (MoM)', 'US', NOW() + INTERVAL '1 day', 'high', '0.2%', '0.3%', 'demo', '{"currency":"USD"}'),
('demo-ecb', 'ECB Rate Decision', 'EU', NOW() + INTERVAL '3 days', 'high', '2.15%', '2.15%', 'demo', '{"currency":"EUR"}'),
('demo-us-retail', 'US Retail Sales', 'US', NOW() + INTERVAL '4 days', 'medium', '0.3%', '0.1%', 'demo', '{"currency":"USD"}'),
('demo-boJ', 'BoJ Rate Decision', 'JP', NOW() + INTERVAL '6 days', 'high', '0.25%', '0.25%', 'demo', '{"currency":"JPY"}'),
('demo-au-employment', 'AU Employment Change', 'AU', NOW() + INTERVAL '8 days', 'medium', '25.0K', '38.5K', 'demo', '{"currency":"AUD"}'),
('demo-uk-cpi', 'UK CPI (YoY)', 'GB', NOW() + INTERVAL '10 days', 'high', '2.4%', '2.3%', 'demo', '{"currency":"GBP"}'),
('demo-us-ppi', 'US PPI (MoM)', 'US', NOW() + INTERVAL '12 days', 'medium', '0.2%', '0.1%', 'demo', '{"currency":"USD"}'),
('demo-de-ifo', 'DE Ifo Business Climate', 'DE', NOW() + INTERVAL '14 days', 'medium', '87.2', '86.5', 'demo', '{"currency":"EUR"}');

INSERT INTO market_data (symbol, timeframe, timestamp, open, high, low, close, source)
SELECT symbol, 'PRICE', NOW() - INTERVAL '5 minutes', price, price, price, price, 'demo'
FROM (VALUES
  ('EURUSD', 1.0875), ('AUDJPY', 98.42), ('USDJPY', 149.35),
  ('SP500', 5325.0), ('XAUUSD', 2388.0), ('XPTUSD', 1012.0),
  ('GER40', 18650.0), ('UK100', 8320.0)
) AS prices(symbol, price);

INSERT INTO structured_opinions
(opinion_id, created_at, opinion_type, scope, direction, confidence, timeframe, summary, key_factors, reasoning, data_inputs, model_used, prompt_version, tokens_used, cost_usd)
VALUES
('11111111-1111-4111-8111-111111111111', NOW() - INTERVAL '14 days', 'macro_regime', 'global_macro', 'neutral', 'moderate', 'multi_week', 'Growth remained resilient while financial conditions were balanced.', '["stable growth","contained volatility"]', 'Deterministic fictional demo analysis.', '{"table":"macro_series","series_ids":["T10Y2Y","VIXCLS","DGS10"]}', 'demo/deterministic', 'macro_regime_v1', 0, 0),
('22222222-2222-4222-8222-222222222222', NOW() - INTERVAL '7 days', 'macro_regime', 'global_macro', 'bullish', 'moderate', 'multi_week', 'Disinflation and improving breadth supported a measured risk-on stance.', '["positive curve","tight credit"]', 'Deterministic fictional demo analysis.', '{"table":"macro_series","series_ids":["T10Y2Y","VIXCLS","BAMLH0A0HYM2"]}', 'demo/deterministic', 'macro_regime_v1', 0, 0),
('33333333-3333-4333-8333-333333333333', NOW() - INTERVAL '1 hour', 'macro_regime', 'global_macro', 'bullish', 'high', 'multi_week', 'The fictional demo economy is in a controlled expansion with constructive risk appetite.', '["positive yield curve","contained volatility","stable inflation expectations"]', 'Deterministic fictional demo analysis.', '{"table":"macro_series","series_ids":["T10Y2Y","VIXCLS","T5YIE","DGS10"]}', 'demo/deterministic', 'macro_regime_v1', 0, 0),
('44444444-4444-4444-8444-444444444444', NOW() - INTERVAL '30 minutes', 'briefing', 'daily_demo', 'mixed', 'high', 'daily', 'Constructive macro backdrop with event risk concentrated around CPI and the ECB.', '["regime","calendar","watchlist"]', 'Deterministic fictional demo briefing.', '{"opinion_ids":["33333333-3333-4333-8333-333333333333"],"event_ids":["demo-us-cpi","demo-ecb"]}', 'demo/deterministic', 'briefing_v3', 0, 0);

INSERT INTO regime_classifications (classification_id, created_at, scope, regime, sub_regime, confidence, supporting_data, opinion_id) VALUES
('51111111-1111-4111-8111-111111111111', NOW() - INTERVAL '14 days', 'global_macro', 'balanced', 'stable_growth', 'moderate', '{}', '11111111-1111-4111-8111-111111111111'),
('52222222-2222-4222-8222-222222222222', NOW() - INTERVAL '7 days', 'global_macro', 'risk_on', 'disinflation', 'moderate', '{}', '22222222-2222-4222-8222-222222222222'),
('53333333-3333-4333-8333-333333333333', NOW() - INTERVAL '1 hour', 'global_macro', 'risk_on', 'controlled_expansion', 'high', '{"caution_flags":["Fictional demo data"],"momentum_implications":"Favor selective risk while respecting event risk."}', '33333333-3333-4333-8333-333333333333');

INSERT INTO daily_briefings (briefing_id, briefing_date, content, sections, opinion_ids, model_used, prompt_version) VALUES
('66666666-6666-4666-8666-666666666666', CURRENT_DATE, 'Deterministic fictional demo briefing.',
'{"macro_trend":"Controlled expansion with contained volatility.","today":"Monitor positioning ahead of fictional CPI.","this_week":"ECB decision is the main cross-asset catalyst.","watchlist_notes":[{"symbol":"EURUSD","bias":"bullish","confidence":"moderate","summary":"Supported by improving relative growth.","note":"Watch the ECB decision and USD inflation."},{"symbol":"DXY","bias":"mixed","confidence":"moderate","summary":"Range-bound into CPI.","note":"A clean inflation surprise could break the range."},{"symbol":"AUDJPY","bias":"bullish","confidence":"moderate","summary":"Constructive risk proxy.","note":"Sensitive to risk appetite."},{"symbol":"USDJPY","bias":"mixed","confidence":"moderate","summary":"Yield support meets intervention risk.","note":"Watch US yields."},{"symbol":"SP500","bias":"bullish","confidence":"high","summary":"Breadth and volatility remain constructive.","note":"Event risk can interrupt the trend."},{"symbol":"XAUUSD","bias":"mixed","confidence":"moderate","summary":"Supported but sensitive to real yields.","note":"CPI is the near-term catalyst."},{"symbol":"XPTUSD","bias":"bullish","confidence":"low","summary":"Industrial demand is improving.","note":"Liquidity remains thinner."},{"symbol":"GER40","bias":"bullish","confidence":"moderate","summary":"Benefits from easing expectations.","note":"ECB guidance matters."},{"symbol":"UK100","bias":"mixed","confidence":"moderate","summary":"Defensive composition offsets currency pressure.","note":"Watch GBP sensitivity."}]}',
ARRAY['33333333-3333-4333-8333-333333333333'::UUID, '44444444-4444-4444-8444-444444444444'::UUID], 'demo/deterministic', 'briefing_v3');

INSERT INTO cycle_runs (correlation_id, status, accepted_at, started_at, completed_at, triggered_by, run_kind, result_status, summary) VALUES
('77777777-7777-4777-8777-777777777777', 'completed', NOW() - INTERVAL '36 minutes', NOW() - INTERVAL '35 minutes', NOW() - INTERVAL '30 minutes', 'demo', 'cycle', 'success', '{"fixture":true}');

INSERT INTO collection_log (started_at, completed_at, collector, status, records_fetched, records_written, duration_ms, api_calls_made, correlation_id) VALUES
(NOW() - INTERVAL '35 minutes', NOW() - INTERVAL '34 minutes', 'fred', 'success', 18, 18, 842, 0, '77777777-7777-4777-8777-777777777777'),
(NOW() - INTERVAL '34 minutes', NOW() - INTERVAL '33 minutes', 'forex_factory', 'success', 8, 8, 514, 0, '77777777-7777-4777-8777-777777777777'),
(NOW() - INTERVAL '33 minutes', NOW() - INTERVAL '32 minutes', 'oanda', 'success', 8, 8, 620, 0, '77777777-7777-4777-8777-777777777777');

INSERT INTO processing_log (started_at, completed_at, processor, status, output_id, model_used, tokens_input, tokens_output, cost_usd, duration_ms, correlation_id) VALUES
(NOW() - INTERVAL '32 minutes', NOW() - INTERVAL '31 minutes', 'macro_regime', 'success', '33333333-3333-4333-8333-333333333333', 'demo/deterministic', 0, 0, 0, 410, '77777777-7777-4777-8777-777777777777'),
(NOW() - INTERVAL '31 minutes', NOW() - INTERVAL '30 minutes', 'briefing', 'success', '44444444-4444-4444-8444-444444444444', 'demo/deterministic', 0, 0, 0, 530, '77777777-7777-4777-8777-777777777777');
