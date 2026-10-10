import 'package:flutter/material.dart';

import '../services/app_state.dart';
import '../services/arabic.dart';
import '../services/hydration.dart';
import '../theme.dart';
import '../widgets/common.dart';
import '../widgets/profile_form.dart';

class SettingsScreen extends StatelessWidget {
  final AppState state;
  const SettingsScreen({super.key, required this.state});

  Future<bool> _confirm(BuildContext context, String title, String body) async {
    final ok = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        icon: const Icon(Icons.warning_amber_rounded, color: AppColors.accent, size: 36),
        title: Text(title),
        content: Text(body),
        actions: [
          TextButton(onPressed: () => Navigator.pop(ctx, false), child: const Text('إلغاء')),
          FilledButton(
            style: FilledButton.styleFrom(backgroundColor: Colors.red.shade400),
            onPressed: () => Navigator.pop(ctx, true),
            child: const Text('تأكيد'),
          ),
        ],
      ),
    );
    return ok ?? false;
  }

  @override
  Widget build(BuildContext context) {
    return ListenableBuilder(
      listenable: state,
      builder: (context, _) {
        final p = state.profile;
        if (p == null) return const SizedBox.shrink();
        return SafeArea(
          child: ListView(
            padding: const EdgeInsets.fromLTRB(18, 12, 18, 24),
            children: [
              const Text('الإعدادات',
                  style: TextStyle(
                      fontSize: 22, fontWeight: FontWeight.bold, color: AppColors.text)),
              const SizedBox(height: 16),
              SectionCard(
                child: Column(children: [
                  Row(children: [
                    CircleAvatar(
                      radius: 28,
                      backgroundColor: AppColors.mint,
                      child: Icon(p.isMale ? Icons.boy : Icons.girl,
                          color: AppColors.primary, size: 34),
                    ),
                    const SizedBox(width: 12),
                    Expanded(
                      child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                        Text(p.name,
                            style: const TextStyle(
                                fontSize: 18, fontWeight: FontWeight.bold)),
                        Text(p.grade, style: const TextStyle(color: AppColors.muted)),
                      ]),
                    ),
                  ]),
                  const Divider(height: 28),
                  Row(mainAxisAlignment: MainAxisAlignment.spaceAround, children: [
                    _info('العمر', '${Ar.n(p.age)} سنة'),
                    _info('الوزن', '${Ar.n(p.weight)} كغ'),
                    _info('الطول', '${Ar.n(p.height)} سم'),
                  ]),
                ]),
              ),
              const SizedBox(height: 12),
              SectionCard(
                padding: const EdgeInsets.symmetric(vertical: 6),
                child: Column(children: [
                  ListTile(
                    leading: const IconBadge(Icons.edit_outlined),
                    title: const Text('تعديل البيانات الشخصية'),
                    subtitle: const Text('الاسم، الصف، العمر، الوزن، الطول'),
                    trailing: const Icon(Icons.chevron_left),
                    onTap: () => Navigator.of(context).push(MaterialPageRoute(
                      builder: (_) => _EditProfilePage(state: state),
                    )),
                  ),
                  ListTile(
                    leading: const IconBadge(Icons.flag_outlined, color: AppColors.water),
                    title: const Text('الهدف اليومي'),
                    subtitle: Text(
                        '${Ar.ml(state.goal)} محسوب تلقائياً وفق معايير صحية موثوقة'),
                  ),
                ]),
              ),
              const SizedBox(height: 12),
              SectionCard(
                child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                  const Row(children: [
                    IconBadge(Icons.calculate_outlined),
                    SizedBox(width: 10),
                    Text('كيف نحسب احتياجك؟',
                        style: TextStyle(fontSize: 16, fontWeight: FontWeight.bold)),
                  ]),
                  const SizedBox(height: 10),
                  _calc('حسب الوزن (طريقة هوليداي وسيغار)',
                      Hydration.hollidaySegar(p.weight)),
                  _calc('حسب مساحة سطح الجسم (الوزن والطول)',
                      Hydration.bsaMethod(p.weight, p.height)),
                  _calc('حسب العمر والجنس (التوصيات الدولية)',
                      Hydration.ageMethod(p.age, p.isMale)),
                  const Divider(),
                  _calc('المتوسط المعتمد هدفاً يومياً', state.goal, bold: true),
                  const SizedBox(height: 6),
                  const Text(
                      'هذه تقديرات تثقيفية عامة، ويُستشار الطبيب في الحالات الصحية الخاصة.',
                      style: TextStyle(fontSize: 12, color: AppColors.muted)),
                ]),
              ),
              const SizedBox(height: 12),
              SectionCard(
                padding: const EdgeInsets.symmetric(vertical: 6),
                child: Column(children: [
                  ListTile(
                    leading: const IconBadge(Icons.delete_sweep_outlined,
                        color: AppColors.accent),
                    title: const Text('مسح سجل الشرب'),
                    subtitle: const Text('حذف كل الكميات المسجلة مع إبقاء بياناتك'),
                    onTap: () async {
                      if (await _confirm(context, 'مسح السجل',
                          'سيتم حذف سجل شرب الماء بالكامل. هل تريد المتابعة؟')) {
                        await state.clearHistory();
                        if (context.mounted) showMessage(context, 'تم مسح السجل بنجاح');
                      }
                    },
                  ),
                  ListTile(
                    leading: IconBadge(Icons.restart_alt, color: Colors.red.shade400),
                    title: Text('إعادة ضبط التطبيق',
                        style: TextStyle(color: Colors.red.shade400)),
                    subtitle: const Text('حذف جميع البيانات والبدء من جديد'),
                    onTap: () async {
                      if (await _confirm(context, 'إعادة ضبط البيانات',
                          'سيتم حذف بيانات الطالب والسجل والإعدادات نهائياً. لا يمكن التراجع عن هذه الخطوة.')) {
                        await state.resetAll();
                      }
                    },
                  ),
                ]),
              ),
              const SizedBox(height: 18),
              const Center(
                child: Text('مذكر شرب الماء المدرسي • الإصدار ١٫٠\nيعمل دون إنترنت وتُحفظ بياناتك على جهازك فقط',
                    textAlign: TextAlign.center,
                    style: TextStyle(color: AppColors.muted, fontSize: 12, height: 1.6)),
              ),
            ],
          ),
        );
      },
    );
  }

  Widget _info(String label, String value) => Column(children: [
        Text(value,
            style: const TextStyle(
                fontWeight: FontWeight.bold, fontSize: 16, color: AppColors.primaryDark)),
        Text(label, style: const TextStyle(color: AppColors.muted, fontSize: 12)),
      ]);

  Widget _calc(String label, int ml, {bool bold = false}) => Padding(
        padding: const EdgeInsets.symmetric(vertical: 4),
        child: Row(children: [
          Expanded(
              child: Text(label,
                  style: TextStyle(fontWeight: bold ? FontWeight.bold : null))),
          Text(Ar.ml(ml),
              style: TextStyle(
                  fontWeight: FontWeight.bold,
                  color: bold ? AppColors.primary : AppColors.text)),
        ]),
      );
}

class _EditProfilePage extends StatelessWidget {
  final AppState state;
  const _EditProfilePage({required this.state});

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: const Text('تعديل البيانات')),
      body: ListView(
        padding: const EdgeInsets.fromLTRB(18, 8, 18, 32),
        children: [
          ProfileForm(
            initial: state.profile,
            submitLabel: 'حفظ التعديلات',
            onSubmit: (p) async {
              await state.saveProfile(p);
              if (context.mounted) {
                showMessage(context, 'تم حفظ البيانات وتحديث هدفك اليومي');
                Navigator.pop(context);
              }
            },
          ),
        ],
      ),
    );
  }
}
