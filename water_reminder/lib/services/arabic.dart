/// أدوات تنسيق عربية بسيطة (أرقام، أيام، أوقات).
class Ar {
  static const _digits = ['٠', '١', '٢', '٣', '٤', '٥', '٦', '٧', '٨', '٩'];

  static String n(num value) {
    final s = value is double && value != value.roundToDouble()
        ? value.toStringAsFixed(1)
        : value.round().toString();
    return s.split('').map((c) {
      final d = int.tryParse(c);
      if (d != null) return _digits[d];
      if (c == '.') return '٫';
      return c;
    }).join();
  }

  static String ml(num v) => '${n(v)} مل';

  static String liters(num v) => '${n((v / 100).round() / 10)} لتر';

  static const weekdays = [
    'الإثنين', 'الثلاثاء', 'الأربعاء', 'الخميس', 'الجمعة', 'السبت', 'الأحد'
  ];
  static const weekdaysShort = ['إثن', 'ثلا', 'أرب', 'خمي', 'جمع', 'سبت', 'أحد'];

  static const months = [
    'يناير', 'فبراير', 'مارس', 'أبريل', 'مايو', 'يونيو',
    'يوليو', 'أغسطس', 'سبتمبر', 'أكتوبر', 'نوفمبر', 'ديسمبر'
  ];

  static String date(DateTime d) =>
      '${weekdays[d.weekday - 1]} ${n(d.day)} ${months[d.month - 1]}';

  static String time(DateTime d) {
    final h = d.hour % 12 == 0 ? 12 : d.hour % 12;
    final m = d.minute.toString().padLeft(2, '0');
    return '${n(h)}:${n(int.parse(m[0]))}${n(int.parse(m[1]))} ${d.hour < 12 ? 'ص' : 'م'}';
  }

  static String minutes(int m) {
    if (m < 60) return '${n(m)} دقيقة';
    final h = m ~/ 60, r = m % 60;
    final hs = h == 1 ? 'ساعة' : (h == 2 ? 'ساعتان' : '${n(h)} ساعات');
    return r == 0 ? hs : '$hs و${n(r)} دقيقة';
  }

  static String days(int d) {
    if (d == 1) return 'يوم واحد';
    if (d == 2) return 'يومان';
    if (d >= 3 && d <= 10) return '${n(d)} أيام';
    return '${n(d)} يوماً';
  }
}
