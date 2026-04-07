# Bao Cao Danh Gia Nguon Tin Va Muc Do "Thong Minh" Hien Tai

Ngay lap: 2026-03-27

## 1. Ket luan nhanh

He thong hien tai da co bo khung rat dung huong cho mot AI digest thuc chien: co ingestion, normalize source/date, dedup, scoring, deep analysis, delivery judge, archive, Telegram. Ve kien truc, day khong con la mot demo prompt nua.

Nhung neu hoi thang theo muc tieu cua sep va cua du an, thi cau tra loi cua toi la:

- Kien truc tong the: on.
- Format Telegram: da o muc kha on.
- Nguon tin: chua on.
- Do rong truy cap thong tin: chua on.
- Do sac cua phan tich Qwen tren input da duoc lam sach: tam on.
- Do manh tong the so voi ChatGPT/Claude/Grok: con khoang cach lon.

Noi gon hon nua:

- He thong cua minh dang manh o `khung xu ly`.
- He thong cua minh dang yeu o `do rong va chat luong retrieval`.
- Qwen hien tai khong "ngu", nhung no chua duoc nuoi bang mot retrieval stack du manh de cho cam giac nhu ChatGPT/Claude that.

## 2. Bang chung chinh tu code va du lieu

### Nguon duoc thiet ke trong code

Theo [nodes/gather_news.py](/Users/quangdang/Projects/daily-digest-agent/nodes/gather_news.py):

- RSS feeds:
  - TechCrunch AI
  - The Verge AI
  - Ars Technica
  - Google AI blog
  - VnExpress Cong nghe
  - Genk AI
- DuckDuckGo search:
  - 7 query tieng Anh ve OpenAI, Anthropic, Google DeepMind, Meta, xAI, Hugging Face, AI breakthrough
  - 4 query tieng Viet ve AI Viet Nam
- Telegram channels:
  - `aivietnam`
  - `MLVietnam`
  - `binhdanhocai`
  - `ai_mastering_vn`
  - `nghienai`

### Nguon dang xuat hien trong database hien tai

Thong ke tu `database.db`:

| Source | So bai |
|---|---:|
| DuckDuckGo (EN) | 51 |
| DuckDuckGo (VN) | 36 |
| RSS: TechCrunch AI | 20 |
| RSS: VnExpress Cong nghe | 18 |
| RSS: Genk AI | 16 |
| RSS: blog.google / AI | 6 |
| RSS: Ars Technica | 1 |

### Domain xuat hien nhieu nhat trong database hien tai

| Domain | So bai |
|---|---:|
| techcrunch.com | 21 |
| vnexpress.net | 19 |
| genk.vn | 16 |
| blog.google | 6 |
| stackoverflow.com | 4 |
| cnbc.com | 3 |
| news.google.com | 3 |
| chouseisan.com | 3 |
| support.google.com | 3 |
| bing.com | 2 |
| arstechnica.com | 2 |
| about.fb.com | 2 |
| news.mit.edu | 2 |
| anthropic.com | 2 |

### Log van hanh quan trong

Theo [digest.log](/Users/quangdang/Projects/daily-digest-agent/digest.log) va [digest_run.log](/Users/quangdang/Projects/daily-digest-agent/digest_run.log):

- Telegram channels dang bi skip vi chua co Telethon credentials.
- Mot run gan day co `57 raw articles`, nhung chi `3 include / 56 total` vao delivery judge.
- Co run Telegram phai dung `safe_fallback`.

Dieu nay cho thay:

- He thong gather duoc kha nhieu.
- Nhung rat nhieu bai la noise, off-scope, hoac khong du manh de brief.
- Retrieval dang lay rong nhung chua lay "sac".

## 3. Danh gia nguon tin hien tai

## Diem manh

- Da co RSS tu mot so nguon uy tin va de auto hoa.
- Da co normalize source/date o [nodes/normalize_source.py](/Users/quangdang/Projects/daily-digest-agent/nodes/normalize_source.py), day la mot nang cap rat dung.
- Da co `source_tier` va `source_verified` de giam viec model tin nguon yeu mot cach mu quang.
- Da co freshness penalty, stale penalty, event clustering, delivery judge. Day la cac lop ma nhieu du an local agent bo qua.

## Diem yeu

- Nguon hien tai bi lech rat manh sang `DuckDuckGo search`.
- DDG la lop lay ket qua tim kiem, khong phai lop source intelligence thuc su.
- So RSS feed con qua it so voi muc tieu "nhin the gioi AI dang co gi moi".
- Chua co ingestion that cho:
  - X
  - Facebook groups
  - Hacker News
  - Reddit API
  - GitHub Releases / changelog feeds
  - cac blog chinh thuc cua OpenAI, Anthropic, Meta, Microsoft, Nvidia, Cohere, Mistral, Perplexity, Databricks, Snowflake, AWS, Cloudflare, v.v.
- Co dau hieu leak nguon khong phai news that:
  - `stackoverflow.com`
  - `support.google.com`
  - `chouseisan.com`
  - `bing.com`
  - `news.google.com`

Noi that: voi ambition cua sep, day chua phai bo nguon cua mot "executive AI intelligence system". Day moi la bo nguon cua mot `good early-stage prototype`.

## 4. Danh gia phan tich cua Qwen hien tai

## Cai da on

- Qwen khong dang bi quang vao bai toan mot cach ngay tho. He thong da boc no bang kha nhieu scaffold tot:
  - source normalization
  - freshness metadata
  - prefilter
  - classification prompt ro
  - grounding note
  - deep analysis structure
  - delivery judge
- Scoring pipeline trong [nodes/classify_and_score.py](/Users/quangdang/Projects/daily-digest-agent/nodes/classify_and_score.py) kha thong minh ve mat san pham:
  - co strategic boost
  - co event clustering
  - co freshness penalty
  - co fallback cho held-out articles de tiet kiem tai nguyen
- Deep analysis trong [nodes/deep_analysis.py](/Users/quangdang/Projects/daily-digest-agent/nodes/deep_analysis.py) da co y thuc tach:
  - fact anchors
  - reasonable inferences
  - unknown / need verification

Noi cach khac: do "thong minh" cua he thong hien tai den nhieu tu kien truc va guardrail, khong chi tu model.

## Cai chua on

- Qwen van phu thuoc rat nang vao chat luong input. Input xau thi output van xau, du prompt co dep den dau.
- Community search trong deep analysis van dung DDG de tim Reddit/HN/news, nen rat mong va thieu on dinh.
- `MAX_CLASSIFY_ARTICLES` mac dinh la 8, nghia la chi 8 bai duoc LLM cham ky; phan con lai dung heuristic fallback. Day tot cho toc do, nhung rat de bo sot tin quan trong neu retrieval chua chuan.
- Prompt classification va deep analysis da tot, nhung van chua co mot lop eval production nghiem tuc de do:
  - tin co that su moi khong
  - scoring co on dinh khong
  - summary co grounded khong
  - recommendation co dung muc do actionability khong

Danh gia thang:

- `Qwen + scaffold` dang o muc co the lam mot digest huu ich.
- Nhung chua o muc "nghien cuu sau tren internet rong, tiep can nhieu lop thong tin, doi chieu lien nguon, tu hieu van de nhu ChatGPT/Claude/Grok".

## 5. Danh gia theo tung goc nhin

## Goc nhin cua sep / founder

Neu toi la sep, toi se thay:

- Rat dang dau tu tiep vi bo khung nay co tu duy dung.
- Nhung chua du de goi la "AI intelligence moat".
- Diem nghen lon nhat khong phai giao dien hay prompt nua, ma la retrieval va source network.

Founder muon:

- biet om lon cong nghe dang lam gi
- biet xu huong nao dang hinh thanh
- biet cai nao dung de ra quyet dinh
- biet startup nho co the chen vao dau

He thong hien tai moi dap ung tot nhat phan:

- tong hop lai mot tap tin duoc thu gom
- cham va sap xep kha co logic

Nó chua dap ung tot phan:

- "di san tim kiem sau"
- "nhin thay tin truoc so dong"
- "nghe duoc hoi tho cong dong va nguoi trong nghe"

## Goc nhin cua quan ly / operator

Tu goc nhin van hanh, he thong kha on:

- co pipeline ro
- co diem cham
- co route deep/basic/skip
- co luu vet Notion + SQLite + memory
- co scheduler

Quan ly se thich vi no `co the quan ly duoc`.

Nhung van co 3 rui ro:

- noise dau vao con cao
- score co the dep tren giay nhung sai nguon
- doc xong van thay "co thong tin" chu chua chac "co intelligence"

## Goc nhin cua nguoi dung pho thong

Nguoi dung pho thong co the thay:

- ban brief de doc
- tieng Viet on
- format de nuot
- tin co ve "xin" hon mot list RSS thong thuong

Voi nhom nay, he thong co the da kha on.

## Goc nhin cua nguoi dung chuyen nghiep

Nguoi dung chuyen nghiep se bat dau kho tinh:

- tai sao nguon nay lai len?
- tin nay co that moi khong?
- sao lai co link StackOverflow / support page / landing page?
- scoring nay dua tren cai gi?
- recommendation nay la grounded hay van la giai thich hay?

Voi nhom nay, he thong hien tai chua du chac.

## Goc nhin cua "AI xin xo" / frontier-level expectation

Neu so voi ky vong ChatGPT/Claude/Grok:

- retrieval breadth: thua xa
- freshness verification: thua xa
- cross-source synthesis: thua xa
- tu nhan biet context rong cua nganh: thua xa
- do ben khi gap input mong/noisy: thua xa

Nhung he thong minh co 2 diem rat dang quy:

- chay local
- kien truc dang di dung huong de nang cap dan

No khong o muc frontier model, nhung o muc `foundation co the nang cap`.

## 6. Danh gia tong hop cua toi

Neu cham thang tay:

| Hang muc | Diem cam tinh |
|---|---:|
| Kien truc pipeline | 8/10 |
| Telegram output / delivery thinking | 7.5/10 |
| Source normalization va freshness logic | 7/10 |
| Source breadth | 4.5/10 |
| Source quality mix | 5.5/10 |
| Deep analysis on grounded input | 6.5/10 |
| Tong the so voi muc tieu "giong ChatGPT/Claude" | 4.5/10 |

So sanh dung cach:

- So voi mot script RSS + summary thong thuong: he thong nay hon ro.
- So voi mot san pham AI executive intelligence ma sep dang hinh dung: chua dat.
- So voi ChatGPT/Claude khi cho phep browse/tool/research day du: con mot khoang rat xa.

## 7. Nhan xet thien vi va thang than

Nhan xet thien vi cua toi la:

- Neu tiep tuc toi uu prompt ma khong nang cap retrieval, ban se gap tran som.
- Neu tiep tuc them scoring rule ma nguon van la DDG-dominant, he thong se trong thong minh hon nhung khong that su thong minh hon.
- Neu muon sep cam thay "con nay bat dau giong ChatGPT/Claude", ban phai danh vao `nguon + tools + eval`, khong phai chi danh vao `model`.

Noi rat thang:

- Qwen hien tai khong phai van de lon nhat.
- Van de lon nhat la he thong chua cho Qwen "nhin" du nhieu thu tot.

## 8. Uu tien de xuat tiep theo

## Uu tien 1: Sua bo nguon

- Them official blog/feed cua OpenAI, Anthropic, Meta AI, Microsoft, Nvidia, Hugging Face, GitHub Releases, AWS AI, Cloudflare, Databricks, Mistral, Cohere.
- Them Hacker News API va Reddit API that, khong chi search qua DDG.
- Them whitelist domain manh thay vi chi blocklist.
- Chan manh hon non-news pages va support/landing pages.

## Uu tien 2: Dung feed phu hop voi sep

- X/Facebook hien chua co API ngon va ben. Cach thuc te nhat la:
  - semi-manual seeding
  - watchlist account/page/group
  - luu vao memory va phan tich sau

## Uu tien 3: Tao dashboard giai thich duoc

- Moi ngay thong ke:
  - raw by source
  - kept by source
  - delivered by source
  - top false positives
  - top missed stories

## Uu tien 4: Eval that

- Tao 30-50 case chuan de cham:
  - do moi
  - do dung nguon
  - do dung type
  - score co hop ly khong
  - actionability co dung muc khong

## 9. Ket luan cuoi

Neu phai noi voi sep bang mot cau ngan:

`He thong da co bo xuong song rat tot, nhung hien tai van manh ve xu ly hon la manh ve truy cap tri thuc; de tien gan ChatGPT/Claude, phai nang retrieval va source intelligence len truoc.`
