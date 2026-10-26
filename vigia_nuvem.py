"""Vigia do ETH na nuvem (sem janela): as mesmas regras do Modo Cripto, rodando 24 h num servidor.
- Manda os avisos pro celular (ntfy) — com o PC ligado ou desligado.
- Sincroniza com o PC pelo tópico <canal>-sync (posição aberta, linhas de aviso, valor da operação).
- Aceita comandos do celular no tópico <canal>-cmd: "entrei 2310 10000", "sai 2390", "stop 2280", "alvo 2400", "status".
Nunca compra nem vende nada.
"""
import json
import re
import threading
import time

import cripto

cripto.ORIGEM = "nuvem"
cripto.mostrar_no_monitor = lambda *a, **k: None      # sem tela no servidor
cripto.minimizar = lambda *a, **k: None
cripto.bipe = lambda: None
log = cripto.log


class Nuvem(cripto.Cripto):
    def avisar(self, titulo, msg, nivel, fala, forte=True):
        super().avisar(titulo, msg, nivel, "", forte)    # sem voz no servidor

    def comandos_celular(self):
        desde = "1m"
        while True:
            try:
                for ev in cripto.ler_topico(self.aj, self.aj["ntfy_topico"] + "-cmd", desde):
                    desde = ev.get("id", desde)
                    self.tratar_comando((ev.get("message") or "").strip())
            except Exception as e:
                log.info("cmd celular: %s", e)
            time.sleep(15)

    def responder(self, titulo, msg):
        cripto.mandar_celular(dict(self.aj, celular=True), titulo, msg, False)

    def tratar_comando(self, t: str):
        tl = t.lower().replace("í", "i").replace("á", "a")
        nums = []
        for m in re.finditer(r"(\d[\d.,]*)\s*(mil|k)?", tl):
            x, mult = m.group(1).rstrip(".,"), m.group(2)
            v = float(x.replace(".", "").replace(",", ".")) if re.fullmatch(r"\d{1,3}(\.\d{3})+(,\d+)?", x) else float(x.replace(",", "."))
            nums.append(v * 1000 if mult else v)
        log.info("comando do celular: %s", t)
        if re.match(r"(entrei|entrar|comprei)", tl):
            r = self.entrar(nums[0] if nums else None, nums[1] if len(nums) > 1 else None)
        elif re.match(r"(sai|vendi|sair)", tl):
            r = self.sair(nums[0] if nums else None)
        elif re.match(r"stop", tl) and nums:
            r = self.set_alvo_stop(stop=nums[0])
        elif re.match(r"alvo", tl) and nums:
            r = self.set_alvo_stop(alvo=nums[0])
        elif re.match(r"(status|como|posi)", tl):
            r = self.info_posicao()
        else:
            self.responder("❓ Comando não entendido", "Use: entrei 2310 10000 · sai 2390 · stop 2280 · alvo 2400 · status")
            return
        if r.get("erro"):
            self.responder("❓ Não deu", r["erro"])
            return
        p = r.get("posicao")
        a = self.ultima or {}
        if r.get("operacao"):
            op = r["operacao"]
            self.responder(f"✅ Saída registrada · {op['lucro']:+,.0f} US$",
                           f"Entrada {op['entrada']:,.2f} → saída {op['saida']:,.2f}, com taxa. Mês: {r['mes_qtd']} op., {r['mes_total']:+,.0f} US$.")
        elif p:
            self.responder(f"🟦 Em operação · {p['lucro']:+,.0f} US$",
                           f"Entrada {p['preco']:,.2f} com {p['valor']:,.0f} US$ · agora {p['atual']:,.2f} · alvo {p.get('alvo') or 0:,.2f} · "
                           f"stop {p.get('stop') or 0:,.2f} · empata em {p['empate']:,.2f}.")
        else:
            self.responder("⏸ Sem operação aberta",
                           f"ETH {a.get('preco', 0):,.2f} · sinal: {a.get('nivel', '?')} · mês: {r['mes_qtd']} op., {r['mes_total']:+,.0f} US$.")


def main():
    app = Nuvem()
    app.inicio = time.time() - 60                          # no servidor não tem abertura para esperar
    log.info("vigia da nuvem ligado (%s), canal %s", app.aj["par"], app.aj["ntfy_topico"])
    threading.Thread(target=app.sincronizar, daemon=True, name="sync").start()
    threading.Thread(target=app.comandos_celular, daemon=True, name="cmd").start()
    app.ciclo()


if __name__ == "__main__":
    main()
