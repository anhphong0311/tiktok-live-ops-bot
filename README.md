# TikTok-to-Zalo LIVE Notification

Bot Windows theo dõi `@bohiubimsua` mỗi 15 giây và thông báo qua Zalo PC khi phiên LIVE bắt đầu hoặc kết thúc. Bot không dùng cookie, token hay mật khẩu Zalo. Trạng thái và outbox được lưu trong SQLite để không gửi trùng sau khi khởi động lại.

Bot cũng có thể cảnh báo khi số mắt LIVE thấp hơn ngưỡng đặt cho từng ngày. Đổi ngưỡng mà không cần khởi động lại bot:

```bat
.venv\Scripts\python.exe -m tiktok_zalo_bot --config config.yaml set-viewer-threshold 130
```

Ngày khác muốn đặt 50 mắt thì chạy lại lệnh với `50`. Bot chỉ cảnh báo sau 2 lần kiểm tra thấp liên tiếp. Nếu mắt vẫn chưa lên, bot nhắc lại sau mỗi `reminder_minutes` (mặc định 15 phút); nếu đã phục hồi ít nhất `recovery_margin` rồi lại tụt, một đợt cảnh báo mới cũng được mở. Tắt riêng cảnh báo mắt bằng:

```bat
.venv\Scripts\python.exe -m tiktok_zalo_bot --config config.yaml disable-viewer-alert
```

## Báo cáo GMV theo giờ

Nhánh dịch vụ `run-gmv` theo dõi cùng `room_id` LIVE nhưng chạy độc lập với bot
thông báo bắt đầu/kết thúc. Trong khi phiên còn LIVE, dịch vụ đọc trang TikTok
Shop Workbench bằng Microsoft Edge đã đăng nhập và gửi vào nhóm `GMV - BHBS`:

- Attributed GMV cộng dồn từ đầu phiên.
- Ads Cost và tỷ lệ `Ads Cost / GMV`.
- Một tổng kết cuối phiên sau khi OFFLINE được xác nhận.

GMV, Ads Cost và tỷ lệ `Ads Cost / GMV` được gửi mỗi 60 phút. Sản phẩm được
kiểm tra riêng mỗi 5 phút: bot chỉ liệt kê tên, ID, GMV tăng thêm và GMV hiện
tại của những sản phẩm thực sự tăng doanh số trong đúng cửa sổ đó. Nếu không có
sản phẩm nào tăng GMV trong 5 phút, bot không gửi tin sản phẩm. Danh sách dài
được tách thành nhiều tin nhưng không làm mất sản phẩm.

Mốc báo cáo được neo theo giờ bắt đầu hiển thị trên dashboard (60, 120, 180
phút...), không neo theo thời điểm bot được bật. Khi không đọc được dashboard,
bot báo lỗi một lần, nhắc lại sau mỗi 30 phút và báo phục hồi khi đọc lại được.

Các lệnh vận hành:

```bat
.venv\Scripts\python.exe -m tiktok_zalo_bot --config config.yaml gmv-status
.venv\Scripts\python.exe -m tiktok_zalo_bot --config config.yaml run-gmv --once
.venv\Scripts\python.exe -m tiktok_zalo_bot --config config.yaml disable-gmv-send
```

Task nền riêng có tên `TikTok-GMV-Hourly-Reporter` và được cài bằng:

```powershell
.\scripts\install_gmv_reporter_task.ps1 -Enable -StartNow
```

Nếu tài khoản Windows không có quyền tạo Scheduled Task, script tự dùng mục
khởi động cùng đăng nhập của người dùng (`HKCU Run`) và vẫn chạy ẩn bằng
`pythonw.exe`.

Hai dịch vụ dùng chung `data\zalo_ui.lock`, vì vậy không thể thao tác Zalo cùng
lúc. Dịch vụ GMV xác minh chính xác tên nhóm qua Windows Accessibility trước khi
điền và gửi tin.

## Cài đặt

Mở PowerShell hoặc Command Prompt trong `C:\TikTok-Zalo-Bot`:

```bat
setup.cmd
.venv\Scripts\python.exe -m pytest
```

`config.yaml` mặc định an toàn: chưa có nhóm, `dry_run: true`, `send_enabled: false`.

## Kiểm tra TikTok

```bat
.venv\Scripts\python.exe -m tiktok_zalo_bot --config config.yaml check-live
```

Kết quả có ba trạng thái:

- `LIVE`: TikTokLive xác nhận đang LIVE. Bot cố lấy `room_id` làm khóa chống trùng.
- `OFFLINE`: TikTokLive trả về `false` một cách bình thường.
- `UNKNOWN`: mất mạng, timeout, CAPTCHA, rate-limit hoặc API thay đổi. Bot **không** coi `UNKNOWN` là `OFFLINE`.

Khi có chuyển trạng thái xác thực `LIVE → OFFLINE`, bot gửi đúng một thông báo kết thúc gồm giờ bắt đầu, giờ kết thúc và thời lượng. Thời gian có độ trễ tối đa xấp xỉ chu kỳ kiểm tra 15 giây. `UNKNOWN` không bao giờ kích hoạt thông báo kết thúc.

TikTok không có public official LIVE API cho nhu cầu này. Dự án dùng TikTokLive 7.0.1 (`TikTokLiveClient.is_live()`), là thư viện reverse-engineered; vì vậy bot giữ trạng thái `UNKNOWN` và backoff khi dịch vụ lỗi.

## Chọn và xác nhận nhóm Zalo

1. Mở Zalo PC đã đăng nhập.
2. Mở chính xác nhóm cần nhận tin.
3. Giữ cửa sổ Zalo ở kích thước cố định, rồi chạy:

```bat
.venv\Scripts\python.exe -m tiktok_zalo_bot --config config.yaml setup-zalo --group-name "TÊN NHÓM CHÍNH XÁC"
```

4. Nhập câu xác nhận được yêu cầu. Làm theo các đếm ngược để chỉ ô tìm kiếm, ô nhập tin và vùng tên nhóm.

Công cụ lưu ảnh vùng tên nhóm. Mỗi lần chạy, bot tìm nhóm rồi so sánh header với ảnh đã xác nhận. Nếu sai nhóm hoặc kích thước cửa sổ thay đổi, bot dừng trước khi điền tin.

## Preview, chưa gửi

```bat
.venv\Scripts\python.exe -m tiktok_zalo_bot --config config.yaml preview
```

Lệnh này chỉ điền nội dung vào ô chat, không nhấn Enter. Ảnh chụp được lưu tại `preview\latest.png`. Hãy kiểm tra tên nhóm và nội dung ngay trong Zalo.

## Bật/tắt gửi thật

Chỉ sau khi preview đúng:

```bat
.venv\Scripts\python.exe -m tiktok_zalo_bot --config config.yaml enable-send
```

Nhập đúng câu `BAT GUI THAT: <tên nhóm>`. Lệnh này mới đổi `send_enabled: true` và `dry_run: false`.

Tắt khẩn cấp:

```bat
.venv\Scripts\python.exe -m tiktok_zalo_bot --config config.yaml disable-send
```

## Vận hành

Chạy liên tục:

```bat
run_bot.cmd
```

Chạy một lần:

```bat
.venv\Scripts\python.exe -m tiktok_zalo_bot --config config.yaml run --once
```

Xem trạng thái, lịch sử và log:

```bat
check_status.cmd
Get-Content .\logs\bot.log -Tail 100
```

Chỉ một tiến trình `run` được phép nhờ `data\bot.lock`. Log xoay vòng 5 MB, giữ 5 bản.

## Chống gửi trùng và lỗi không xác định

- `room_id` là khóa chống trùng khi lấy được. Nếu không có, bot giữ một session key qua các lần `UNKNOWN` và qua restart.
- Thông báo bắt đầu và kết thúc có hai khóa chống trùng riêng. Giờ bắt đầu của phiên đang chạy được giữ trong SQLite qua restart.
- Trước khi nhấn Enter, bot commit `SENDING` vào SQLite. Nếu process dừng đúng lúc này, lần chạy sau chuyển thành `UNCERTAIN` và **không tự gửi lại**.
- Sau khi kiểm tra Zalo thủ công, xử lý bản ghi `UNCERTAIN`:

```bat
.venv\Scripts\python.exe -m tiktok_zalo_bot --config config.yaml resolve --notification-id 1 --outcome sent
.venv\Scripts\python.exe -m tiktok_zalo_bot --config config.yaml resolve --notification-id 1 --outcome not-sent
```

`not-sent` chỉ cho phép retry vì người vận hành đã xác nhận tin chưa được gửi. Lỗi chắc chắn xảy ra trước Enter được retry tối đa 3 lần.

## Windows Task Scheduler

Zalo automation cần desktop tương tác, vì vậy task phải chạy **Only when user is logged on**. Sau khi cài đặt, setup nhóm và preview thành công, mở PowerShell:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\register_task.ps1
Start-ScheduledTask -TaskName "TikTok-Zalo-Live-Notifier"
```

Task khởi động khi user đăng nhập, retry sau 1 phút nếu process thoát. Không chọn `Run whether user is logged on or not`, vì Zalo PC sẽ không có desktop để thao tác.

Task dùng `pythonw.exe` nên chạy ẩn, không mở cửa sổ CMD. Xem hoạt động bằng `check_status.cmd` hoặc file `logs\bot.log`.

Ngoài trigger khi đăng nhập, task có watchdog mỗi phút. Nếu tiến trình bot bị dừng, Windows tự chạy lại trong khoảng một phút; khi bot vẫn đang chạy, Task Scheduler bỏ qua lần kích hoạt mới để không tạo tiến trình trùng.

Khi cần dừng bot:

```powershell
Stop-ScheduledTask -TaskName "TikTok-Zalo-Live-Notifier"
```

Khi phát hiện LIVE, bot tự mở Zalo PC từ `zalo.executable_path` nếu cửa sổ chưa có. Trước mỗi thao tác chuột, bàn phím và trước khi nhấn Enter, bot xác minh cửa sổ foreground thuộc đúng tiến trình `Zalo.exe`. Nếu Zalo không lên trước, Windows đang khóa, chưa đăng nhập hoặc tên nhóm không khớp ảnh xác nhận, bot dừng an toàn và không gõ sang ứng dụng khác.

## Dữ liệu nhạy cảm

Dự án không lưu mật khẩu, cookie hoặc token Zalo. `config.yaml` chỉ chứa tên nhóm, tọa độ UI và cờ an toàn. SQLite chỉ chứa trạng thái LIVE, nội dung thông báo và kết quả gửi.

