import 'dart:math';

import '../models/profile.dart';

/// حساب الاحتياج اليومي من الماء بالاعتماد على ثلاث طرق صحية معتمدة
/// ثم أخذ متوسطها للحصول على تقدير متوازن:
///
/// 1. طريقة هوليداي-سيغار (الوزن): 100 مل/كغ لأول 10 كغ،
///    ثم 50 مل/كغ للعشرة التالية، ثم 20 مل/كغ لما يزيد.
/// 2. مساحة سطح الجسم (معادلة موستيلر، الوزن والطول): 1500 مل لكل متر مربع.
/// 3. الكمية الكافية حسب العمر والجنس (الهيئة الأوروبية لسلامة الغذاء
///    والأكاديمية الوطنية الأمريكية)، ونحتسب منها ما يأتي من المشروبات (80٪).
class Hydration {
  static int hollidaySegar(double w) {
    if (w <= 10) return (w * 100).round();
    if (w <= 20) return (1000 + (w - 10) * 50).round();
    return (1500 + (w - 20) * 20).round();
  }

  static double bodySurfaceArea(double weight, double height) =>
      sqrt(height * weight / 3600);

  static int bsaMethod(double weight, double height) =>
      (bodySurfaceArea(weight, height) * 1500).round();

  static int ageMethod(int age, bool isMale) {
    double total;
    if (age <= 3) {
      total = 1300;
    } else if (age <= 8) {
      total = 1700;
    } else if (age <= 13) {
      total = isMale ? 2400 : 2100;
    } else if (age <= 18) {
      total = isMale ? 3300 : 2300;
    } else {
      total = isMale ? 3700 : 2700;
    }
    return (total * 0.8).round();
  }

  /// الهدف اليومي بالمليلتر، مقرباً لأقرب 50 مل.
  static int dailyGoal(StudentProfile p) {
    final avg = (hollidaySegar(p.weight) +
            bsaMethod(p.weight, p.height) +
            ageMethod(p.age, p.isMale)) /
        3;
    final clamped = avg.clamp(1000, 3500);
    return ((clamped / 50).round() * 50);
  }

  /// الحصة المقترحة خلال اليوم الدراسي (نحو 40٪ من الهدف).
  static int schoolShare(int goal) => ((goal * 0.4) / 50).round() * 50;

  /// فترة التذكير التلقائية بالدقائق: توزيع الهدف على أكواب سعة 200 مل
  /// خلال ساعات اليقظة (نحو 14 ساعة)، بين 30 و120 دقيقة.
  static int autoIntervalMinutes(int goal) {
    final cups = max(1, (goal / 200).ceil());
    final minutes = (14 * 60 / cups).round();
    final clamped = minutes.clamp(30, 120);
    return (clamped / 5).round() * 5;
  }
}
