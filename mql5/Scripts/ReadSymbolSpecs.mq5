//+------------------------------------------------------------------+
//| ReadSymbolSpecs.mq5                                              |
//|                                                                  |
//| Le as especificacoes do simbolo DO SERVIDOR e grava JSON em       |
//| RISER-data. Nao decide nada, nao corrige nada, nao negocia.       |
//|                                                                  |
//| Por que este script vem antes de qualquer outra coisa em MQL5:    |
//|                                                                  |
//| Os manifestos de config/brokers/ declaram digits, contract_oz,    |
//| stops level e swap. Nenhum desses numeros foi lido do servidor —  |
//| foram escritos a partir do que a corretora anuncia. Se digits for |
//| 2 e nao 3, o ponto vale dez vezes mais, e TODA conversao do       |
//| projeto sai dez vezes errada: swap, spread, distancia de stop,    |
//| degrau de trailing. E sai plausivel, que e o que torna o erro     |
//| caro — a mesma classe do ask/bid trocado no .bi5 e do mes com     |
//| base zero da Dukascopy.                                          |
//|                                                                  |
//| O JSON produzido aqui e a MEDICAO. O manifesto e a DECLARACAO.   |
//| Compara-los e trabalho do verificador em Python, e divergencia    |
//| entre os dois e erro, nao aviso. Este script nao corrige o        |
//| manifesto: corrigir automaticamente transformaria uma divergencia |
//| que alguem precisa ver numa edicao silenciosa de arquivo          |
//| versionado.                                                       |
//+------------------------------------------------------------------+
#property script_show_inputs
#property strict

// Vazio = simbolo do grafico. Nenhum simbolo literal aparece neste arquivo, e
// nao pode aparecer (invariante 3): o simbolo se resolve em runtime.
input string InpSymbol   = "";      // Simbolo (vazio = o do grafico)
input bool   InpVerbose  = true;    // Ecoar o JSON no log de Experts

// Codigos de erro sao codigos, nunca frases (docs/schemas/log-schema.md).
#define E_SYMBOL_NOT_FOUND   "E1003"
#define E_ACCOUNT_NOT_LOGGED "E2003"
#define E_FILE_WRITE_FAILED  "E5004"

//+------------------------------------------------------------------+
//| Hash do login. Numero de conta nunca em claro, nem em nome de     |
//| diretorio. Tem de casar byte a byte com                          |
//| riser.core.paths.hash_account_login — sha256 do login em ASCII,   |
//| hex minusculo, truncado em 12 caracteres. Se os dois divergirem,  |
//| o Python grava num diretorio e o MQL5 noutro, e ninguem percebe   |
//| ate faltar arquivo.                                               |
//+------------------------------------------------------------------+
string HashLogin(const long login)
  {
   string      texto = IntegerToString(login);
   uchar       src[], key[], dst[];
   StringToCharArray(texto, src, 0, StringLen(texto), CP_UTF8);

   if(CryptEncode(CRYPT_HASH_SHA256, src, key, dst) <= 0)
      return("");

   string hex = "";
   for(int i = 0; i < 6 && i < ArraySize(dst); i++)
      hex += StringFormat("%02x", dst[i]);
   return(hex);
  }

//+------------------------------------------------------------------+
//| Escapa string para JSON. Nome de corretora e de servidor vem do   |
//| servidor e pode conter qualquer coisa; uma aspa solta produz um   |
//| arquivo que o verificador recusa a ler, e o motivo aparece longe  |
//| da causa.                                                         |
//+------------------------------------------------------------------+
string JsonStr(const string s)
  {
   string out = "";
   int n = StringLen(s);
   for(int i = 0; i < n; i++)
     {
      ushort c = StringGetCharacter(s, i);
      if(c == '"')       out += "\\\"";
      else if(c == '\\') out += "\\\\";
      else if(c == '\n') out += "\\n";
      else if(c == '\r') out += "\\r";
      else if(c == '\t') out += "\\t";
      else if(c < 0x20)  out += StringFormat("\\u%04x", c);
      else               out += ShortToString(c);
     }
   return("\"" + out + "\"");
  }

string JsonNum(const double v, const int casas = 10)
  {
   return(DoubleToString(v, casas));
  }

//+------------------------------------------------------------------+
//| Decodificadores de enum. O NUMERO cru vai junto no JSON: o nome   |
//| existe para quem le, o numero para quem compara. Guardar so o     |
//| nome apostaria que esta tabela esta completa em toda build        |
//| futura do terminal.                                               |
//+------------------------------------------------------------------+
string SwapModeName(const long v)
  {
   switch((int)v)
     {
      case 0: return("DISABLED");
      case 1: return("POINTS");
      case 2: return("CURRENCY_SYMBOL");
      case 3: return("CURRENCY_MARGIN");
      case 4: return("CURRENCY_DEPOSIT");
      case 5: return("INTEREST_CURRENT");
      case 6: return("INTEREST_OPEN");
      case 7: return("REOPEN_CURRENT");
      case 8: return("REOPEN_BID");
     }
   return("DESCONHECIDO");
  }

string DayName(const long v)
  {
   switch((int)v)
     {
      case 0: return("sunday");
      case 1: return("monday");
      case 2: return("tuesday");
      case 3: return("wednesday");
      case 4: return("thursday");
      case 5: return("friday");
      case 6: return("saturday");
     }
   return("DESCONHECIDO");
  }

string MarginModeName(const long v)
  {
   switch((int)v)
     {
      case 0: return("netting");
      case 1: return("exchange");
      case 2: return("hedging");
     }
   return("DESCONHECIDO");
  }

string TradeModeName(const long v)
  {
   switch((int)v)
     {
      case 0: return("disabled");
      case 1: return("longonly");
      case 2: return("shortonly");
      case 3: return("closeonly");
      case 4: return("full");
     }
   return("DESCONHECIDO");
  }

string CalcModeName(const long v)
  {
   switch((int)v)
     {
      case 0: return("forex");
      case 1: return("futures");
      case 2: return("cfd");
      case 3: return("cfdindex");
      case 4: return("cfdleverage");
      case 5: return("forex_no_leverage");
     }
   return("OUTRO");
  }

//+------------------------------------------------------------------+
//| SYMBOL_FILLING_MODE e mascara de bits, nao valor. Ler como valor  |
//| daria "3" e ninguem saberia que sao FOK e IOC juntos.             |
//+------------------------------------------------------------------+
string FillingList(const long mask)
  {
   string itens = "";
   if((mask & SYMBOL_FILLING_FOK) != 0) itens += "\"FOK\",";
   if((mask & SYMBOL_FILLING_IOC) != 0) itens += "\"IOC\",";
   if((mask & 4) != 0)                  itens += "\"BOC\",";  // build recente
   int n = StringLen(itens);
   if(n > 0) itens = StringSubstr(itens, 0, n - 1);
   return("[" + itens + "]");
  }

string SourceName()
  {
   long modo = AccountInfoInteger(ACCOUNT_TRADE_MODE);
   if(modo == ACCOUNT_TRADE_MODE_DEMO)    return("demo");
   if(modo == ACCOUNT_TRADE_MODE_CONTEST) return("contest");
   return("live");
  }

//+------------------------------------------------------------------+
bool GravarTexto(const string caminho, const string conteudo)
  {
   int h = FileOpen(caminho, FILE_WRITE | FILE_TXT | FILE_ANSI, 0, CP_UTF8);
   if(h == INVALID_HANDLE)
     {
      PrintFormat("%s FileOpen('%s') falhou (erro %d).",
                  E_FILE_WRITE_FAILED, caminho, GetLastError());
      return(false);
     }
   FileWriteString(h, conteudo);
   FileClose(h);
   return(true);
  }

//+------------------------------------------------------------------+
void OnStart()
  {
   //--- simbolo ------------------------------------------------------
   string sym = InpSymbol;
   if(StringLen(sym) == 0)
      sym = _Symbol;

   if(!SymbolSelect(sym, true))
     {
      PrintFormat("%s simbolo '%s' nao existe neste servidor ou nao pode ser "
                  "selecionado. Nada foi gravado.", E_SYMBOL_NOT_FOUND, sym);
      return;
     }

   //--- conta --------------------------------------------------------
   // O diretorio de destino depende do LOGIN, nao do terminal. Junction e por
   // terminal; conta e por login, e o mesmo terminal troca de conta sem que
   // nada no sistema de arquivos mude (ADR 0002). Sem login nao ha destino.
   long login = AccountInfoInteger(ACCOUNT_LOGIN);
   if(login == 0)
     {
      PrintFormat("%s terminal sem conta conectada. O destino depende do login; "
                  "gravar na raiz misturaria contas com custo diferente.",
                  E_ACCOUNT_NOT_LOGGED);
      return;
     }

   string hash = HashLogin(login);
   if(StringLen(hash) != 12)
     {
      PrintFormat("%s falha ao derivar o hash do login (CryptEncode).",
                  E_ACCOUNT_NOT_LOGGED);
      return;
     }

   //--- leitura ------------------------------------------------------
   long   digits        = SymbolInfoInteger(sym, SYMBOL_DIGITS);
   double point         = SymbolInfoDouble(sym, SYMBOL_POINT);
   double contract      = SymbolInfoDouble(sym, SYMBOL_TRADE_CONTRACT_SIZE);
   double tick_value    = SymbolInfoDouble(sym, SYMBOL_TRADE_TICK_VALUE);
   double tick_value_p  = SymbolInfoDouble(sym, SYMBOL_TRADE_TICK_VALUE_PROFIT);
   double tick_value_l  = SymbolInfoDouble(sym, SYMBOL_TRADE_TICK_VALUE_LOSS);
   double tick_size     = SymbolInfoDouble(sym, SYMBOL_TRADE_TICK_SIZE);
   long   stops_level   = SymbolInfoInteger(sym, SYMBOL_TRADE_STOPS_LEVEL);
   long   freeze_level  = SymbolInfoInteger(sym, SYMBOL_TRADE_FREEZE_LEVEL);
   double swap_long     = SymbolInfoDouble(sym, SYMBOL_SWAP_LONG);
   double swap_short    = SymbolInfoDouble(sym, SYMBOL_SWAP_SHORT);
   long   swap_3days    = SymbolInfoInteger(sym, SYMBOL_SWAP_ROLLOVER3DAYS);
   long   swap_mode     = SymbolInfoInteger(sym, SYMBOL_SWAP_MODE);
   long   filling       = SymbolInfoInteger(sym, SYMBOL_FILLING_MODE);
   double vol_min       = SymbolInfoDouble(sym, SYMBOL_VOLUME_MIN);
   double vol_max       = SymbolInfoDouble(sym, SYMBOL_VOLUME_MAX);
   double vol_step      = SymbolInfoDouble(sym, SYMBOL_VOLUME_STEP);
   long   trade_mode    = SymbolInfoInteger(sym, SYMBOL_TRADE_MODE);
   long   calc_mode     = SymbolInfoInteger(sym, SYMBOL_TRADE_CALC_MODE);
   long   spread_now    = SymbolInfoInteger(sym, SYMBOL_SPREAD);
   bool   spread_float  = (bool)SymbolInfoInteger(sym, SYMBOL_SPREAD_FLOAT);
   string cur_base      = SymbolInfoString(sym, SYMBOL_CURRENCY_BASE);
   string cur_profit    = SymbolInfoString(sym, SYMBOL_CURRENCY_PROFIT);
   string cur_margin    = SymbolInfoString(sym, SYMBOL_CURRENCY_MARGIN);
   long   margin_mode   = AccountInfoInteger(ACCOUNT_MARGIN_MODE);

   double bid = SymbolInfoDouble(sym, SYMBOL_BID);
   double ask = SymbolInfoDouble(sym, SYMBOL_ASK);

   //--- JSON ---------------------------------------------------------
   // run_id existe para que duas leituras da mesma conta em dias diferentes
   // sejam atribuiveis (invariante 6). build_hash do fonte NAO entra: o MQL5
   // nao consegue calcular o hash do proprio arquivo, e inventar um campo
   // vazio chamado build_hash seria pior que nao ter — pareceria preenchido.
   string run_id = StringFormat("%s-%d", TimeToString(TimeGMT(), TIME_DATE | TIME_SECONDS),
                                GetTickCount());
   StringReplace(run_id, ".", "");
   StringReplace(run_id, ":", "");
   StringReplace(run_id, " ", "T");

   string j = "{\n";
   j += "  \"schema\": \"symbol-specs/1\",\n";
   j += "  \"run_id\": " + JsonStr(run_id) + ",\n";
   j += "  \"lido_em_utc\": " + JsonStr(TimeToString(TimeGMT(), TIME_DATE | TIME_SECONDS)) + ",\n";
   j += "  \"terminal_build\": " + IntegerToString(TerminalInfoInteger(TERMINAL_BUILD)) + ",\n";
   j += "  \"symbol\": " + JsonStr(sym) + ",\n";

   j += "  \"account\": {\n";
   j += "    \"hash\": " + JsonStr(hash) + ",\n";
   j += "    \"source\": " + JsonStr(SourceName()) + ",\n";
   j += "    \"company\": " + JsonStr(AccountInfoString(ACCOUNT_COMPANY)) + ",\n";
   j += "    \"server\": " + JsonStr(AccountInfoString(ACCOUNT_SERVER)) + ",\n";
   j += "    \"currency\": " + JsonStr(AccountInfoString(ACCOUNT_CURRENCY)) + ",\n";
   j += "    \"margin_mode\": " + JsonStr(MarginModeName(margin_mode)) + ",\n";
   j += "    \"margin_mode_raw\": " + IntegerToString(margin_mode) + "\n";
   j += "  },\n";

   j += "  \"specs\": {\n";
   j += "    \"digits\": " + IntegerToString(digits) + ",\n";
   j += "    \"point\": " + JsonNum(point) + ",\n";
   j += "    \"trade_contract_size\": " + JsonNum(contract, 4) + ",\n";
   j += "    \"trade_tick_value\": " + JsonNum(tick_value) + ",\n";
   j += "    \"trade_tick_value_profit\": " + JsonNum(tick_value_p) + ",\n";
   j += "    \"trade_tick_value_loss\": " + JsonNum(tick_value_l) + ",\n";
   j += "    \"trade_tick_size\": " + JsonNum(tick_size) + ",\n";
   j += "    \"trade_stops_level\": " + IntegerToString(stops_level) + ",\n";
   j += "    \"trade_freeze_level\": " + IntegerToString(freeze_level) + ",\n";
   j += "    \"trade_mode\": " + JsonStr(TradeModeName(trade_mode)) + ",\n";
   j += "    \"trade_calc_mode\": " + JsonStr(CalcModeName(calc_mode)) + ",\n";
   j += "    \"volume_min\": " + JsonNum(vol_min, 4) + ",\n";
   j += "    \"volume_max\": " + JsonNum(vol_max, 4) + ",\n";
   j += "    \"volume_step\": " + JsonNum(vol_step, 4) + ",\n";
   j += "    \"filling_mode\": " + FillingList(filling) + ",\n";
   j += "    \"filling_mode_raw\": " + IntegerToString(filling) + ",\n";
   j += "    \"currency_base\": " + JsonStr(cur_base) + ",\n";
   j += "    \"currency_profit\": " + JsonStr(cur_profit) + ",\n";
   j += "    \"currency_margin\": " + JsonStr(cur_margin) + "\n";
   j += "  },\n";

   // swap_mode decide a UNIDADE de swap_long e swap_short. Sem ele, os dois
   // numeros nao significam nada: em POINTS sao pontos, em CURRENCY_DEPOSIT
   // sao dinheiro da conta, em INTEREST_* sao porcentagem ao ano. Gravar os
   // valores sem o modo seria gravar um numero sem unidade.
   j += "  \"swap\": {\n";
   j += "    \"long\": " + JsonNum(swap_long, 6) + ",\n";
   j += "    \"short\": " + JsonNum(swap_short, 6) + ",\n";
   j += "    \"mode\": " + JsonStr(SwapModeName(swap_mode)) + ",\n";
   j += "    \"mode_raw\": " + IntegerToString(swap_mode) + ",\n";
   j += "    \"rollover3days\": " + JsonStr(DayName(swap_3days)) + ",\n";
   j += "    \"rollover3days_raw\": " + IntegerToString(swap_3days) + "\n";
   j += "  },\n";

   // Instantaneo, nao medido. Existe como conferencia cruzada de digits: com
   // o spread anunciado conhecido, o valor em pontos aqui denuncia um digits
   // errado por um fator de dez. NAO serve para preencher o manifesto — spread
   // medido vem do coletor, ao longo de dias e por horario.
   j += "  \"instantaneo\": {\n";
   j += "    \"bid\": " + JsonNum(bid) + ",\n";
   j += "    \"ask\": " + JsonNum(ask) + ",\n";
   j += "    \"spread_points\": " + IntegerToString(spread_now) + ",\n";
   j += "    \"spread_float\": " + (spread_float ? "true" : "false") + ",\n";
   j += "    \"spread_derivado_por_digits\": " + JsonNum((ask - bid)) + "\n";
   j += "  }\n";
   j += "}\n";

   //--- gravacao -----------------------------------------------------
   // Files\RISER e junction para RISER-data\mt5\<alias>. Nada e gravado na
   // raiz: o nivel da conta e criado antes, depois de ler o login.
   string simples = sym;
   StringReplace(simples, "\\", "_");
   StringReplace(simples, "/", "_");
   StringReplace(simples, ".", "_");

   string dir = "RISER\\" + hash + "\\symbol-specs";
   if(!FolderCreate(dir))
     {
      // ERR_DIRECTORY_ALREADY_EXISTS nao e falha.
      if(GetLastError() != 5019 && !FolderCreate(dir))
        {
         PrintFormat("%s nao foi possivel criar '%s' (erro %d).",
                     E_FILE_WRITE_FAILED, dir, GetLastError());
         return;
        }
      ResetLastError();
     }

   string alvo_hist = dir + "\\" + simples + "-" + run_id + ".json";
   string alvo_ult  = dir + "\\" + simples + "-latest.json";

   if(!GravarTexto(alvo_hist, j) || !GravarTexto(alvo_ult, j))
      return;

   if(InpVerbose)
      Print(j);

   PrintFormat("specs de '%s' gravadas. historico: %s | estavel: %s",
               sym, alvo_hist, alvo_ult);
   PrintFormat("digits=%d point=%s contract=%s swap_mode=%s -> confira com "
               "config/brokers/ pelo verificador em Python. Divergencia e erro.",
               (int)digits, JsonNum(point), JsonNum(contract, 2),
               SwapModeName(swap_mode));
  }
//+------------------------------------------------------------------+
