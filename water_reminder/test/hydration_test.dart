import 'package:flutter_test/flutter_test.dart';
import 'package:water_reminder/models/profile.dart';
import 'package:water_reminder/services/hydration.dart';

void main() {
  test('حساب الاحتياج لطالب عمره ١١ سنة ووزنه ٣٥ كغ', () {
    const p = StudentProfile(
        name: 'أحمد', grade: 'الصف الخامس', isMale: true, age: 11, weight: 35, height: 140);
    final goal = Hydration.dailyGoal(p);
    expect(goal, inInclusiveRange(1500, 2200));
    expect(goal % 50, 0);
  });

  test('فترة التذكير ضمن الحدود', () {
    expect(Hydration.autoIntervalMinutes(1000), inInclusiveRange(30, 120));
    expect(Hydration.autoIntervalMinutes(3500), inInclusiveRange(30, 120));
  });
}
