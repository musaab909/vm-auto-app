import 'package:flutter/material.dart';

import '../models/profile.dart';
import '../services/arabic.dart';
import '../services/hydration.dart';
import '../theme.dart';
import 'common.dart';

const grades = [
  'الصف الأول', 'الصف الثاني', 'الصف الثالث', 'الصف الرابع',
  'الصف الخامس', 'الصف السادس', 'الصف السابع', 'الصف الثامن',
  'الصف التاسع', 'الصف العاشر', 'الصف الحادي عشر', 'الصف الثاني عشر',
];

/// نموذج بيانات الطالب (يُستخدم في التسجيل وفي تعديل الإعدادات).
class ProfileForm extends StatefulWidget {
  final StudentProfile? initial;
  final String submitLabel;
  final ValueChanged<StudentProfile> onSubmit;
  const ProfileForm(
      {super.key, this.initial, required this.submitLabel, required this.onSubmit});

  @override
  State<ProfileForm> createState() => _ProfileFormState();
}

class _ProfileFormState extends State<ProfileForm> {
  final _formKey = GlobalKey<FormState>();
  late final TextEditingController _name;
  late String _grade;
  late bool _isMale;
  late double _age, _weight, _height;

  @override
  void initState() {
    super.initState();
    final p = widget.initial;
    _name = TextEditingController(text: p?.name ?? '');
    _grade = p?.grade ?? grades[4];
    _isMale = p?.isMale ?? true;
    _age = (p?.age ?? 11).toDouble();
    _weight = p?.weight ?? 35;
    _height = p?.height ?? 140;
  }

  @override
  void dispose() {
    _name.dispose();
    super.dispose();
  }

  StudentProfile get _current => StudentProfile(
        name: _name.text.trim(),
        grade: _grade,
        isMale: _isMale,
        age: _age.round(),
        weight: _weight.roundToDouble(),
        height: _height.roundToDouble(),
      );

  Widget _slider({
    required String label,
    required IconData icon,
    required double value,
    required double min,
    required double max,
    required String unit,
    required ValueChanged<double> onChanged,
  }) {
    return Padding(
      padding: const EdgeInsets.only(top: 8),
      child: Column(children: [
        Row(children: [
          Icon(icon, color: AppColors.primary, size: 20),
          const SizedBox(width: 8),
          Text(label, style: const TextStyle(fontWeight: FontWeight.w600)),
          const Spacer(),
          Container(
            padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 4),
            decoration: BoxDecoration(
                color: AppColors.mint, borderRadius: BorderRadius.circular(10)),
            child: Text('${Ar.n(value.round())} $unit',
                style: const TextStyle(
                    color: AppColors.primaryDark, fontWeight: FontWeight.bold)),
          ),
        ]),
        Row(children: [
          _roundBtn(Icons.remove, () => onChanged((value - 1).clamp(min, max))),
          Expanded(
            child: Slider(
              value: value.clamp(min, max),
              min: min,
              max: max,
              divisions: (max - min).round(),
              label: Ar.n(value.round()),
              onChanged: onChanged,
            ),
          ),
          _roundBtn(Icons.add, () => onChanged((value + 1).clamp(min, max))),
        ]),
      ]),
    );
  }

  Widget _roundBtn(IconData i, VoidCallback onTap) => InkResponse(
        onTap: onTap,
        radius: 22,
        child: Container(
          width: 30,
          height: 30,
          decoration: BoxDecoration(
            shape: BoxShape.circle,
            border: Border.all(color: AppColors.primary.withValues(alpha: 0.4)),
          ),
          child: Icon(i, size: 18, color: AppColors.primary),
        ),
      );

  @override
  Widget build(BuildContext context) {
    final goal = Hydration.dailyGoal(_current);
    return Form(
      key: _formKey,
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          SectionCard(
            child: Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
              TextFormField(
                controller: _name,
                textInputAction: TextInputAction.done,
                decoration: const InputDecoration(
                  labelText: 'اسم الطالب',
                  hintText: 'اكتب الاسم الكامل',
                  prefixIcon: Icon(Icons.person_outline),
                ),
                validator: (v) => (v == null || v.trim().length < 2)
                    ? 'الرجاء إدخال اسم صحيح من حرفين على الأقل'
                    : null,
              ),
              const SizedBox(height: 14),
              DropdownButtonFormField<String>(
                initialValue: _grade,
                decoration: const InputDecoration(
                  labelText: 'الصف الدراسي',
                  prefixIcon: Icon(Icons.school_outlined),
                ),
                items: grades
                    .map((g) => DropdownMenuItem(value: g, child: Text(g)))
                    .toList(),
                onChanged: (v) => setState(() => _grade = v!),
              ),
              const SizedBox(height: 14),
              SegmentedButton<bool>(
                segments: const [
                  ButtonSegment(value: true, label: Text('طالب'), icon: Icon(Icons.boy)),
                  ButtonSegment(value: false, label: Text('طالبة'), icon: Icon(Icons.girl)),
                ],
                selected: {_isMale},
                onSelectionChanged: (s) => setState(() => _isMale = s.first),
              ),
            ]),
          ),
          const SizedBox(height: 14),
          SectionCard(
            child: Column(children: [
              _slider(
                  label: 'العمر',
                  icon: Icons.cake_outlined,
                  value: _age,
                  min: 5,
                  max: 19,
                  unit: 'سنة',
                  onChanged: (v) => setState(() => _age = v)),
              _slider(
                  label: 'الوزن',
                  icon: Icons.monitor_weight_outlined,
                  value: _weight,
                  min: 15,
                  max: 120,
                  unit: 'كغ',
                  onChanged: (v) => setState(() => _weight = v)),
              _slider(
                  label: 'الطول',
                  icon: Icons.height,
                  value: _height,
                  min: 90,
                  max: 200,
                  unit: 'سم',
                  onChanged: (v) => setState(() => _height = v)),
            ]),
          ),
          const SizedBox(height: 14),
          Container(
            padding: const EdgeInsets.all(16),
            decoration: BoxDecoration(
              gradient: const LinearGradient(
                  colors: [AppColors.primary, AppColors.water]),
              borderRadius: BorderRadius.circular(20),
            ),
            child: Row(children: [
              const Icon(Icons.water_drop, color: Colors.white, size: 34),
              const SizedBox(width: 12),
              Expanded(
                child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                  const Text('احتياجك اليومي المقترح',
                      style: TextStyle(color: Colors.white70)),
                  Text('${Ar.ml(goal)}  (${Ar.liters(goal)})',
                      style: const TextStyle(
                          color: Colors.white,
                          fontSize: 20,
                          fontWeight: FontWeight.bold)),
                  Text('منها نحو ${Ar.ml(Hydration.schoolShare(goal))} خلال الدوام المدرسي',
                      style: const TextStyle(color: Colors.white, fontSize: 12)),
                ]),
              ),
            ]),
          ),
          const SizedBox(height: 18),
          FilledButton.icon(
            icon: const Icon(Icons.check_circle_outline),
            label: Text(widget.submitLabel),
            onPressed: () {
              if (_formKey.currentState!.validate()) widget.onSubmit(_current);
            },
          ),
        ],
      ),
    );
  }
}
