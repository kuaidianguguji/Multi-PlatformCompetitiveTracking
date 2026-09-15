from competitive_tracking.platforms.mercado.collector import MercadoCollector

# 添加平台时实现 Collector.collect(targets)，然后登记工厂；调度/飞书层无需修改。
COLLECTORS = {"mercado": MercadoCollector}
