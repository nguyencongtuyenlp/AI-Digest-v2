# Roadmap Nang Cap Daily Digest Agent

Ngay lap: 2026-03-27

## Muc tieu toi uu hoa

Muc tieu khong phai la "lam model to hon" hay "viet prompt dep hon", ma la dua he thong tien gan hon toi trai nghiem:

- tim tin moi that su quan trong
- thay duoc cac tin ma founder/sep thuc su quan tam
- loc noise tot
- phan tich ra duoc gia tri thuc te va co hoi
- de sau nay co the dung chung bo xuong song nay cho cac agent khac

## Nguyen tac uu tien

1. Nang `retrieval va source intelligence` truoc.
2. Nang `do do luong` truoc khi nang them prompt.
3. Nang `chat luong quyet dinh` truoc khi nang `do dai output`.
4. Uu tien nhung thay doi co the kiem chung bang log, score, va output that.

## Tong quan 4 chang

### Chang 1: Sua nguon de giam noise va tang do moi
Thoi gian goi y: 3-5 ngay lam viec
Tac dong: cao nhat / chi phi vua

Muc tieu:
- bot rac
- them nguon chinh thuc
- lay duoc tin founder-grade hon

Viec can lam:
- Mo rong RSS/official feeds:
  - OpenAI blog/news
  - Anthropic news/blog
  - Meta AI / about.fb.com AI
  - Microsoft AI blog
  - Nvidia blog/news
  - Hugging Face blog/changelog
  - GitHub Releases cho cac repo/model/tools quan trong
  - AWS, Cloudflare, Databricks, Cohere, Mistral, Perplexity
- Thiet lap `SOURCE_WHITELIST` va `SOURCE_PRIORITY`.
- Chan manh hon non-news pages:
  - support pages
  - landing pages
  - StackOverflow / generic help pages
  - search result wrappers
- Tach query theo theme:
  - model release
  - product launch
  - startup/funding
  - regulation
  - dev tools/open-source
- Them rule "official source first, media source second, search-discovered source last".

Deliverable:
- gather_news cleaner
- normalize_source block/whitelist ro hon
- bao cao source coverage moi

KPI:
- giam ro link non-news len Telegram
- tang ty le bai den tu official/strong media sources
- tang ty le bai co `source_tier = a/b`

### Chang 2: Them social/community intelligence that
Thoi gian goi y: 4-7 ngay
Tac dong: rat cao / kho vua

Muc tieu:
- giam su phu thuoc vao DDG
- bat duoc "hoi tho" cua nganh
- tiep can gan hon cach sep dang tu tim tren X/Facebook/group

Viec can lam:
- Them Hacker News API that.
- Them Reddit API that cho mot so subreddit:
  - LocalLLaMA
  - MachineLearning
  - OpenAI
  - Anthropic
  - singularity / AI startup / agent-related groups neu hop
- Nang Telegram channels ingestion that neu can credentials.
- Tao `watchlist sources`:
  - accounts / blogs / channels ma sep hay theo doi
  - pages / groups / newsletter quan trong
- Co the chon huong practical cho X/Facebook:
  - semi-manual seed input
  - watchlist links
  - clip/import thang vao he thong de Qwen phan tich

Luu y:
- X/Facebook rat kho lam "full auto ben" neu khong co API phu hop.
- Huong khon ngoan nhat la `hybrid ingestion`, khong nen co chap "phai full auto 100% ngay".

Deliverable:
- module community ingestion rieng
- field `community_signal_strength`
- field `watchlist_hit`

KPI:
- moi brief co it nhat 1-2 tin co tinh chat "nguoi trong nghe dang noi toi"
- giam cam giac brief chi toan tin media tong hop

### Chang 3: Bien scoring thanh he thong de giai thich duoc
Thoi gian goi y: 3-5 ngay
Tac dong: cao / chi phi thap-vua

Muc tieu:
- de bao cao sep
- de debug
- de biet score dung hay sai

Viec can lam:
- Log ro score breakdown:
  - source score
  - freshness score
  - startup-fit score
  - project-fit score
  - community score
  - event consensus score
- Tach `delivery score` va `archive score`.
- Them `why surfaced` va `why skipped`.
- Tao report moi ngay:
  - raw by source
  - kept by source
  - deep-analyzed by source
  - delivered by source
  - top false positives
  - top stale catches

Deliverable:
- report markdown/json moi ngay
- Notion fields de nhin giai thich score

KPI:
- khi sep hoi "tai sao tin nay len" co the tra loi ro rang
- khi thay tin do, debug duoc trong 2-3 phut

### Chang 4: Eval va judge de nang chat luong that
Thoi gian goi y: 5-8 ngay
Tac dong: rat cao / chi phi vua

Muc tieu:
- khong doan mo prompt
- co vong lap cai tien that

Viec can lam:
- Tao bo eval 30-50 case that tu du lieu cua minh:
  - tin moi nhung khong quan trong
  - tin cu nhung nguon manh
  - tin official product launch
  - tin startup / funding
  - tin regulation
  - tin community buzz nhung evidence yeu
- Cham cac truong:
  - type dung khong
  - freshness dung khong
  - co dang len Telegram khong
  - recommendation co overclaim khong
- Tao 1 judge rubric rieng:
  - groundedness
  - actionability
  - founder relevance
  - novelty

Deliverable:
- eval suite
- regression checks
- scorecard sau moi lan tune

KPI:
- giam false positive
- giam stale leak
- tang do on dinh cua brief qua nhieu ngay

## 3 huong di de chon

## Huong A: Lean va nhanh

Muc tieu:
- cai thien ro output trong 1 tuan

Lam:
- Chang 1
- mot phan Chang 3

Khong lam ngay:
- social ingestion sau
- eval day du

Phu hop khi:
- muon co ket qua nhin thay ngay
- muon sep thay chat luong brief tang nhanh

Rui ro:
- van chua "giong ChatGPT/Claude" nhieu
- van thieu hoi tho cong dong va watchlist founder

## Huong B: Can bang, de nghi nhat

Muc tieu:
- nang chat luong that, khong chi dep output

Lam:
- Chang 1
- Chang 2 o muc practical
- Chang 3
- mot phan Chang 4

Phu hop khi:
- muon vua tang brief, vua dung duoc de bao cao sep
- muon tao bo xuong song that cho agent khac sau nay

Rui ro:
- mat them cong hon Huong A
- can ky luat lam tung lop cho chac

## Huong C: Ambitious, xay nen tang intelligence

Muc tieu:
- tien sat muc tieu "executive AI intelligence system"

Lam day du:
- Chang 1
- Chang 2
- Chang 3
- Chang 4
- them watchlist founder intelligence layer
- them dashboard daily quality report

Phu hop khi:
- xac dinh day la nen tang lau dai
- sep san sang dau tu them thoi gian de lam dung

Rui ro:
- ton suc
- neu khong co eval va discipline thi de bi lam lon ma loang

## De xuat cua toi

Toi de xuat di theo `Huong B`.

Ly do:
- Huong A thi nhanh, nhung chi sua phan ngoai nhieu hon sua cai cot loi.
- Huong C thi dung ly tuong, nhung de bi qua tay som.
- Huong B la diem can bang tot nhat:
  - tang chat luong nguon that
  - co them hoi tho cong dong
  - co he thong giai thich score
  - bat dau co eval

Neu di Huong B, thu tu toi muon lam se la:

1. Lam sach va mo rong official sources.
2. Them HN API + Reddit API + watchlist seed.
3. Tao score/explainability report.
4. Tao eval suite nho truoc.

## Roadmap 2 tuan goi y

### Tuan 1

Ngay 1-2:
- mo rong RSS va official feeds
- blocklist/whitelist nguon
- chan non-news pages

Ngay 3:
- them HN API
- them Reddit API co ban

Ngay 4:
- them watchlist seed format cho sep/user bo sung link quan tam
- gan community signal vao pipeline

Ngay 5:
- refactor score explanation report
- xuat report markdown moi ngay

### Tuan 2

Ngay 6-7:
- tao eval set dau tien
- test score va delivery decision

Ngay 8-9:
- tune scoring va delivery judge dua tren eval

Ngay 10:
- run thu nghiem 2-3 ngay du lieu
- review voi sep:
  - nguon nao len
  - tin nao duoc chon
  - false positive nao con ton tai

## Dinh nghia "xong" cho giai doan nang cap nay

Toi xem la xong khi:

- brief khong con hay len link non-news vo duyen
- tan suat tin official / strong media tang ro
- co duoc it nhat 1 lop community intelligence that
- moi tin len brief co ly do ro rang
- co bo eval ban dau de khong tune mu
- sep doc brief thay "co intelligence hon", khong chi "co tong hop"

## Viec toi co the lam ngay tiep theo

Neu chot di tiep, toi se bat dau bang:

1. Lam chang 1: nang cap bo nguon va bo loc nguon.
2. Tao report source coverage tu dong moi ngay.
3. Sau do moi sang chang 2.

Day la thu tu co ty le loi ich / cong suc tot nhat.
