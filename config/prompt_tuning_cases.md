# Prompt Tuning Cases

Muc tieu cua file nay:
- Dung 10 case that tu `database.db` de tune prompt cho `Qwen local`.
- Giu cho scoring, deep analysis, va note summary on dinh voi data thuc cua team.
- Bien prompt thanh "editorial system" thay vi prompt chung chung.

## Cach dung

1. Chay prompt hien tai tren tung case.
2. So ket qua voi `expected_*` ben duoi.
3. Neu model sai lap lai cung mot kieu, sua prompt thay vi patch code.
4. Sau moi vong tune, uu tien test lai cac case `skip` va `deep` truoc.

## Mau loi can san

- `skip` thanh `basic`: thuong do model bi cuon theo tieu de, khong nhan ra day la category page/landing page.
- `basic` thanh `deep`: thuong do model overrate relevance voi bai co tu khoa AI nhung thieu bang chung.
- `deep` thanh `basic`: thuong do model bo qua suc nang cua nguon goc, impact, hoac do thi truong.
- Summary qua chung chung: chua neu du 3 y "ban chat / gia tri thuc te / gioi han".
- Deep analysis bi hype: khong tach ro fact va claim.

## 10 Case That

### Case 01
- Title: `Khoa hoc - Cong nghe - Bao Nhan Dan dien tu`
- Source: `DuckDuckGo (VN)`
- URL: `https://nhandan.vn/khoahoc-congnghe/`
- Expected tier: `skip`
- Expected type: `Practical`
- Why: day la category page, khong phai bai viet cu the; content gia tri thap cho digest.

### Case 02
- Title: `Google AI : Google ra mat tinh nang tim kiem AI tai Viet Nam:`
- Source: `DuckDuckGo (VN)`
- URL: `https://vtv.vn/google-ai.html`
- Expected tier: `basic`
- Expected type: `Product`
- Why: co gia tri thi truong VN, nhung can can nhac vi domain/tieu de khong du de deep research neu content mong.

### Case 03
- Title: `Cong nghe - Cap nhat tin Cong nghe moi nhat 24/7`
- Source: `DuckDuckGo (VN)`
- URL: `https://vietnamnet.vn/cong-nghe`
- Expected tier: `skip`
- Expected type: `Practical`
- Why: trang chuyen muc, khong phai tin cu the.

### Case 04
- Title: `LotusHacks 2026: He sinh thai AI Viet Nam du suc canh tranh ...`
- Source: `DuckDuckGo (VN)`
- URL: `https://baotuyenquang.com.vn/khoa-hoc-cong-nghe/202603/lotushacks-2026-he-sinh-thai-ai-viet-nam-du-suc-canh-tranh-trong-khu-vuc-52f6361/`
- Expected tier: `basic`
- Expected type: `Society`
- Why: lien quan he sinh thai AI Viet Nam, phu hop de dua vao digest, nhung tac dong va do manh nguon chua du de phan tich sau.

### Case 05
- Title: `Startup viet nam : Startup "muon" nguon luc xa hoi de tien ...`
- Source: `DuckDuckGo (VN)`
- URL: `https://vtv.vn/startup-viet-nam.html`
- Expected tier: `skip`
- Expected type: `Business`
- Why: khong ro day co phai tin AI hay khong; de model over-score neu chi dua vao tu "startup".

### Case 06
- Title: `Ban do tri tue nhan tao Viet 2025: Sinh vien quoc te can biet ...`
- Source: `DuckDuckGo (VN)`
- URL: `https://www.discovervietnamworld.com/business/ban-do-tri-tue-nhan-tao-viet-2025-sinh-vien-quoc-te-can-biet-gi.html`
- Expected tier: `skip`
- Expected type: `Society`
- Why: topic nghe co ve lien quan, nhung domain yeu va kha nang la bai tong hop/seo cao.

### Case 07
- Title: `China's open-source dominance threatens US AI lead, US ...`
- Source: `DuckDuckGo (EN)`
- URL: `https://www.reuters.com/business/autos-transportation/chinas-open-source-dominance-threatens-us-ai-lead-us-advisory-body-warns-2026-03-23/`
- Expected tier: `deep`
- Expected type: `Business`
- Why: nguon manh, chu de chien luoc, tac dong rong den canh tranh AI va open-source.

### Case 08
- Title: `China is winning the open source AI race - The New Stack`
- Source: `DuckDuckGo (EN)`
- URL: `https://thenewstack.io/china-leads-open-ai-models/`
- Expected tier: `basic`
- Expected type: `Business`
- Why: lien quan va co goc nhin hay, nhung la secondary commentary; dung de bo sung context hon la deep research chinh.

### Case 09
- Title: `AI Breakthroughs, Our Most Advanced Glasses, and More: Meta's 2025 ...`
- Source: `DuckDuckGo (EN)`
- URL: `https://about.fb.com/news/2025/12/ai-breakthroughs-advanced-ai-glasses-meta-2025-highlights/`
- Expected tier: `basic`
- Expected type: `Product`
- Why: primary source nhung mang mau PR/tong ket, khong nen overrate nhu tin nong.

### Case 10
- Title: `Google partners with Agile Robots, growing its AI robotics ...`
- Source: `DuckDuckGo (EN)`
- URL: `https://www.cnbc.com/2026/03/24/google-agile-robots-ai-robotics.html`
- Expected tier: `deep`
- Expected type: `Business`
- Why: nguon manh, co cong ty cu the, co y nghia chien luoc voi robotics va AI infrastructure.

## Tuning Checklist

- Scoring prompt:
  - Model co phat hien `category page` va `landing page` khong?
  - Model co ha C1 khi `content_available=false` khong?
  - Model co dung `source_verified` nhu mot tin hieu, thay vi la chan ly tuyet doi, khong?

- Deep analysis prompt:
  - Co tach `fact` va `claim` ro rang khong?
  - Co noi ve `friction / chi phi / gioi han` khong?
  - Co tranh bịa `phan ung cong dong` khong?

- Note summary prompt:
  - Co bat dau dung bang `Y chinh cua tin nay la:` khong?
  - Co du 3 lop gia tri trong 1 doan khong?
  - Co tran lan thanh ban deep analysis rut gon khong?
