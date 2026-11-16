import argparse
import base64
import json
import os
from pathlib import Path
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, urlopen

TOKEN_URL = "https://icdaccessmanagement.who.int/connect/token"
BASE_URL = "https://id.who.int/icd/release/11"


def carregar_credenciais(arquivo_env):
    """Lê as duas credenciais do ambiente ou de um arquivo .env local."""
    valores = {}
    caminho = Path(arquivo_env)
    if caminho.is_file():
        for linha in caminho.read_text(encoding="utf-8-sig").splitlines():
            if linha.strip() and not linha.lstrip().startswith("#") and "=" in linha:
                chave, valor = linha.split("=", 1)
                valores[chave.strip()] = valor.strip().strip("\"'")
    client_id = os.getenv("WHO_ICD_CLIENT_ID") or valores.get("WHO_ICD_CLIENT_ID")
    secret = os.getenv("WHO_ICD_CLIENT_SECRET") or valores.get("WHO_ICD_CLIENT_SECRET")
    if not client_id or not secret:
        raise RuntimeError("Credenciais ausentes. Preencha o .env ou as variáveis de ambiente.")
    return client_id, secret


def requisitar_json(url, cabecalhos, dados=None):
    """Executa GET (ou POST quando há dados) e interpreta o JSON retornado."""
    pedido = Request(url, headers=cabecalhos, data=dados)
    try:
        with urlopen(pedido, timeout=10) as resposta:
            resultado = json.load(resposta)
    except HTTPError as erro:
        status = erro.code
        erro.close()
        raise RuntimeError(f"HTTP {status}: a requisição não foi concluída.") from None
    except (URLError, TimeoutError, OSError):
        raise RuntimeError("Falha de conexão ou tempo de espera excedido.") from None
    except (ValueError, UnicodeError):
        raise RuntimeError("A resposta recebida não é um JSON válido.") from None
    if not isinstance(resultado, dict):
        raise RuntimeError("Formato inesperado: era esperado um objeto JSON.")
    if resultado.get("error"):
        raise RuntimeError("A OMS indicou um erro no conteúdo da resposta.")
    return resultado


def obter_token(client_id, secret):
    """Troca client_id e secret por um token OAuth2; não imprime o token."""
    autorizacao = base64.b64encode(f"{client_id}:{secret}".encode()).decode()
    dados = urlencode({"grant_type": "client_credentials", "scope": "icdapi_access"}).encode()
    resposta = requisitar_json(TOKEN_URL, {
        "Authorization": "Basic " + autorizacao,
        "Content-Type": "application/x-www-form-urlencoded",
    }, dados)
    token = resposta.get("access_token")
    if not isinstance(token, str) or not token:
        raise RuntimeError("O servidor de autenticação não retornou um token válido.")
    return token


def consultar_uri(uri, token, release="2026-01", idioma="en"):
    """Consulta uma URI oficial MMS da edição escolhida e devolve o JSON da OMS."""
    partes = urlsplit(uri)
    prefixo = f"/icd/release/11/{release}/mms/"
    if (partes.scheme not in {"http", "https"} or partes.netloc != "id.who.int"
            or not partes.path.startswith(prefixo) or partes.query or partes.fragment
            or ".." in partes.path or "%" in partes.path):
        raise RuntimeError("Use uma URI oficial ICD-11 MMS do release configurado.")
    return requisitar_json("https://id.who.int" + partes.path, {
        "Authorization": "Bearer " + token, "API-Version": "v2",
        "Accept": "application/json", "Accept-Language": idioma,
    })


def consultar_codigo(codigo, token, release="2026-01", idioma="en"):
    """Resolve o código na OMS e consulta os detalhes da entidade principal."""
    url = f"{BASE_URL}/{release}/mms/codeinfo/{quote(codigo, safe='')}"
    informacoes = requisitar_json(url, {
        "Authorization": "Bearer " + token, "API-Version": "v2",
        "Accept": "application/json", "Accept-Language": idioma,
    })
    entidade = None
    if informacoes.get("stemId"):
        time.sleep(0.2)  # Política de polidez proposta; não é limite oficial da OMS.
        entidade = consultar_uri(informacoes["stemId"], token, release, idioma)
    return {"codigo_consultado": codigo, "informacoes_codigo": informacoes,
            "entidade_principal": entidade}


def buscar_texto(texto, token, release="2026-01", idioma="en", flexivel=False):
    """Busca candidatos e preserva os campos da OMS, inclusive score e matchingPVs."""
    parametros = urlencode({"q": texto, "useFlexisearch": str(flexivel).lower(),
                            "highlightingEnabled": "false", "medicalCodingMode": "true"})
    return requisitar_json(f"{BASE_URL}/{release}/mms/search?{parametros}", {
        "Authorization": "Bearer " + token, "API-Version": "v2",
        "Accept": "application/json", "Accept-Language": idioma,
    })


def extrair_texto(valor):
    """Extrai o texto de um campo JSON-LD, sem os blocos de idioma."""
    if isinstance(valor, str):
        return valor
    if isinstance(valor, dict):
        return extrair_texto(valor.get("@value", valor.get("label", "")))
    return ""


def organizar_entidade(entidade, codigo=None):
    """Apresenta codigo, titulo, termos e parent como listas sem repeticoes."""
    termos = entidade.get("indexTerm") or []
    pais = entidade.get("parent") or []
    if isinstance(pais, str):
        pais = [pais]
    codigo = codigo or entidade.get("code") or entidade.get("theCode")
    titulo = extrair_texto(entidade.get("title"))
    return {
        "codigo": [codigo] if codigo else [],
        "titulo": [titulo] if titulo else [],
        "termos": list(dict.fromkeys(texto for termo in termos
                                     if (texto := extrair_texto(termo)))),
        "parent": list(dict.fromkeys(pais)),
    }


def main():
    """Lê o comando, autentica, executa uma operação e imprime o JSON."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operacao", choices=["codigo", "uri", "busca"])
    parser.add_argument("valor", help="Código ICD-11, URI MMS ou texto para buscar")
    parser.add_argument("--env", default=str(Path(__file__).with_name(".env")))
    parser.add_argument("--release", default="2026-01")
    parser.add_argument("--idioma", default="en")
    parser.add_argument("--flexivel", action="store_true", help="Usar somente com busca")
    parser.add_argument("--bruto", action="store_true", help="Mostrar a resposta completa da OMS")
    args = parser.parse_args()
    if args.flexivel and args.operacao != "busca":
        parser.error("--flexivel só se aplica à operação busca")
    try:
        client_id, secret = carregar_credenciais(args.env)
        token = obter_token(client_id, secret)
        if args.operacao == "codigo":
            resultado = consultar_codigo(args.valor, token, args.release, args.idioma)
        elif args.operacao == "uri":
            resultado = consultar_uri(args.valor, token, args.release, args.idioma)
        else:
            resultado = buscar_texto(args.valor, token, args.release, args.idioma, args.flexivel)
        if not args.bruto:
            if args.operacao == "codigo":
                codigo = resultado["informacoes_codigo"].get("code") or resultado["codigo_consultado"]
                resultado = organizar_entidade(resultado.get("entidade_principal") or {}, codigo)
            elif args.operacao == "uri":
                resultado = organizar_entidade(resultado)
            else:
                def organizar_candidato(candidato):
                    item = organizar_entidade(candidato)
                    if "score" in candidato:
                        item["score"] = candidato["score"]
                    if candidato.get("descendants"):
                        item["descendentes"] = [organizar_candidato(filho)
                                                for filho in candidato["descendants"]]
                    return item
                resultado = {
                    "resultados": [organizar_candidato(item)
                                   for item in resultado.get("destinationEntities") or []],
                    "resultados_truncados": bool(resultado.get("resultChopped")),
                }
        print(json.dumps(resultado, ensure_ascii=False, indent=2))
        return 0
    except (RuntimeError, OSError) as erro:
        print(json.dumps({"erro": str(erro)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())