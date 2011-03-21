#!/bin/bash
# Internal use, see documentation_system.rst
rm -rf build ordocs.tgz _templates/examples.html _templates/databases.html _templates/database_generator_template.rst
mkdir -p build
curdir=`pwd`
cd ..
rootdir=`pwd`
cd $curdir
doxycommands="STRIP_FROM_PATH        = $rootdir
PROJECT_NUMBER = `openrave-config --version`
ALIASES += openraveversion=`openrave-config --version`
"
echo "$doxycommands" | cat Doxyfile.html Doxyfile.en - > build/Doxyfile.html.en
echo "$doxycommands" | cat Doxyfile.latex Doxyfile.en - > build/Doxyfile.latex.en
echo "$doxycommands" | cat Doxyfile.html Doxyfile.ja - > build/Doxyfile.html.ja
echo "$doxycommands" | cat Doxyfile.latex Doxyfile.ja - > build/Doxyfile.latex.ja

bash build_images.sh

# doxygen
python build_doxygen.py --lang=en
cp build/en/latex/refman.pdf build/en/openrave.pdf

python build_doxygen.py --lang=ja
#cp ja/latex/refman.pdf build/ja/openrave_ja.pdf

# build internal openravepy docs
python build_openravepy_internal.py --languagecode en --languagecode ja
# have to rebuild openravepy_int to get correct docstrings!

# build openravepy documentation
prevlang=$LANG
export LANG=en_US.UTF-8
./build_sphinx.sh en
export LANG=ja_JP.UTF-8
./build_sphinx.sh ja
export LANG=$prevlang