/// بيانات الطالب الشخصية.
class StudentProfile {
  final String name;
  final String grade;
  final bool isMale;
  final int age; // بالسنوات
  final double weight; // بالكيلوغرام
  final double height; // بالسنتيمتر

  const StudentProfile({
    required this.name,
    required this.grade,
    required this.isMale,
    required this.age,
    required this.weight,
    required this.height,
  });

  StudentProfile copyWith({
    String? name,
    String? grade,
    bool? isMale,
    int? age,
    double? weight,
    double? height,
  }) =>
      StudentProfile(
        name: name ?? this.name,
        grade: grade ?? this.grade,
        isMale: isMale ?? this.isMale,
        age: age ?? this.age,
        weight: weight ?? this.weight,
        height: height ?? this.height,
      );

  Map<String, dynamic> toJson() => {
        'name': name,
        'grade': grade,
        'isMale': isMale,
        'age': age,
        'weight': weight,
        'height': height,
      };

  factory StudentProfile.fromJson(Map<String, dynamic> j) => StudentProfile(
        name: j['name'] as String,
        grade: j['grade'] as String,
        isMale: j['isMale'] as bool? ?? true,
        age: (j['age'] as num).toInt(),
        weight: (j['weight'] as num).toDouble(),
        height: (j['height'] as num).toDouble(),
      );
}

/// سجل شربة واحدة.
class DrinkEntry {
  final DateTime time;
  final int ml;
  const DrinkEntry(this.time, this.ml);

  Map<String, dynamic> toJson() => {'t': time.millisecondsSinceEpoch, 'ml': ml};
  factory DrinkEntry.fromJson(Map<String, dynamic> j) =>
      DrinkEntry(DateTime.fromMillisecondsSinceEpoch(j['t'] as int), j['ml'] as int);
}
